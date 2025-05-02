import asyncio
import ipaddress
import logging
import ssl
import struct
import traceback
from asyncio import StreamReader, StreamWriter, Task
from collections import defaultdict
from collections.abc import Coroutine, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("IrcClient")

CHANNEL_PREFIXES = "&#+!"


def is_channel_name(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in CHANNEL_PREFIXES)


class ServerTerminationError(Exception):
    pass


@dataclass
class IrcRawMessage:
    prefix: str | None
    command: str
    params: list[str]
    secure: bool = False
    ts: datetime = field(default_factory=datetime.now)

    @property
    def prefix_nick(self) -> str:
        if self.prefix and "!" in self.prefix:
            return self.prefix.split("!")[0]
        return self.prefix or ""

    @property
    def prefix_host(self) -> str:
        if self.prefix and "!" in self.prefix:
            return self.prefix.split("!")[1]
        return ""


class Buffer[T]:
    """Fixed-length circular buffer"""

    def __init__(self, size: int | None = None):
        self.size = size or 1024
        self._buf: list[T] = []
        self.len = 0
        self.idx = 0

    def insert(self, obj: T) -> None:
        if self.len < self.size:
            self._buf.append(obj)
            self.len += 1
        else:
            self._buf[self.idx] = obj
        self.idx = (self.idx + 1) % self.size

    def __len__(self) -> int:
        return self.len

    def __iter__(self) -> Iterator[T]:
        """Generator yields LIFO, i.e. newest first"""
        for i in range(self.len):
            yield self._buf[(self.idx - i - 1) % self.size]

    def fixed_iter(self, start: int) -> Iterator[T]:
        """Iterates previous items from a fixed start-point, independent of working index"""
        if self.len == self.size:
            if start <= self.idx:
                readable = start + self.len - self.idx
            else:
                readable = start - self.idx
        else:
            # buffer not yet full
            start = readable = min(start, self.idx)

        for i in range(readable):
            yield self._buf[(start - i - 1) % self.size]


class IrcChannel:
    def __init__(self, name: str, buf_size: int | None = None):
        self.buf = Buffer[IrcRawMessage](buf_size)
        self.name = name
        self.mode = ""
        self.topic = ""
        self.topic_origin: tuple[str, str] = ("", "")
        self.users: set[str] = set()


class ChannelDict(defaultdict[str, IrcChannel]):
    def __missing__(self, key: str) -> IrcChannel:
        self[key] = value = IrcChannel(key)
        return value


@dataclass
class DCC:
    source: str
    filename: str
    size: int
    ip: str
    port: int
    start_time: datetime
    end_time: datetime | None = None
    ssl: bool = False
    bytes_received: int = 0
    verified: bool = False

    @property
    def complete(self) -> bool:
        return self.size == self.bytes_received

    def __str__(self) -> str:
        return f'DCC for "{self.filename}" from "{self.source}" ({self.ip}:{self.port})'


class IrcClient:
    """
    Basic IRC Client based on
        - https://datatracker.ietf.org/doc/html/rfc2812#section-3.7.3
        - https://datatracker.ietf.org/doc/html/draft-oakley-irc-ctcp-02
        - https://www.alien.net.au/irc/irc2numerics.html
    """

    version = "Kirk 0.1.0 (python)"
    encryption_marker = "~"

    def __init__(
        self,
        host: str,
        nick: str,
        auto_join: Sequence[str] | None = None,
        auth: str | None = None,
        port: int | None = None,
        ssl: bool = False,
        keys: dict[str, str] | None = None,
        dcc_dir: str | None = None,
        log_mode: Literal["file", "console", "none"] = "none",
    ):
        self.host = host
        self.port = port or 6697 if ssl else 6667
        self.nick = nick
        self.servers: list[str] = []
        self.mode: set[str] = set()
        self.auto_join = auto_join or []
        self.auth = auth
        self.ssl = ssl
        self.dcc_dir = dcc_dir
        self.keys = keys or {}
        self.channels = ChannelDict()
        self.chats = defaultdict[str, Buffer[IrcRawMessage]](Buffer)
        self.dcc: list[DCC] = []
        self.log_mode = log_mode
        self.log_buf = Buffer[IrcRawMessage](512)
        self._reader: StreamReader = None  # type: ignore[assignment]
        self._writer: StreamWriter = None  # type: ignore[assignment]
        self._futures = set[Future[Any] | Task[Any]]()
        if log_mode == "file":
            self._fh = open(f"kirk_{datetime.now().isoformat()}.log", "w")  # noqa: SIM115

    @property
    def server_buf(self) -> Buffer[IrcRawMessage]:
        return self.chats[self.server_buf_name]

    @property
    def server_buf_name(self) -> str:
        return self.host

    def get_buf(self, name: str) -> Buffer[IrcRawMessage]:
        if name == self.host or name in self.servers:
            return self.server_buf
        elif is_channel_name(name):
            return self.channels[name].buf
        else:
            return self.chats[name]

    async def delete(self, name: str) -> None:
        if name == self.host or name in self.servers:
            pass
        elif is_channel_name(name):
            await self.part_channel(name)
            del self.channels[name]
        else:
            del self.chats[name]

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            host=self.host,
            port=self.port,
            ssl=self.ssl,
        )
        self.mode.clear()
        # introduce ourselves to the server
        await self.change_nick(self.nick)
        await self.send_cmd("USER", [self.nick, "0", "*"], self.nick)
        # fork off delayed post-connect tasks
        if self.auth:
            self._delay(self.perform_auth())
        if self.auto_join:
            self._delay(self.perform_auto_join())

    async def perform_auth(self) -> None:
        # wait for establishing boilerplate to end. mode from server is usually the end
        while not self.mode:
            await asyncio.sleep(1)

        await asyncio.sleep(1)
        await self.send_message("NickServ", f"IDENTIFY {self.nick} {self.auth}")

    async def perform_auto_join(self) -> None:
        # if given, wait for auth to complete (r = registered)
        while not self.mode or (self.auth and "r" not in self.mode):
            await asyncio.sleep(1)

        await asyncio.sleep(2)
        for channel in self.auto_join:
            await self.join_channel(channel)

    async def _send_raw(self, raw: str) -> None:
        self.log(raw, "OUT")
        self._writer.write((raw + "\r\n").encode("utf-8"))
        await self._writer.drain()

    async def send_cmd(self, command: str, params: str | list[str] = "", trailing: str = "") -> None:
        if isinstance(params, list):
            params = " ".join(params)
        params = f" {params}" if params else ""
        trailing = f" :{trailing}" if trailing else ""
        await self._send_raw(f"{command}{params}{trailing}")

    async def join_channel(self, channel: str, password: str | None = None) -> None:
        await self.send_cmd("JOIN", channel)

    async def part_channel(self, channel: str, reason: str = "") -> None:
        await self.send_cmd("PART", channel, reason)

    async def change_nick(self, nick: str) -> None:
        await self.send_cmd("NICK", nick)

    async def quit(self, text: str = "") -> None:
        await self.send_cmd("QUIT", [], text)

    async def list(self) -> None:
        await self.send_cmd("LIST")

    async def send_message(self, recipient: str, text: str, encrypt: bool = False) -> None:
        if encrypt:
            if not (key := self.keys.get(recipient)):
                self.log_error(f"{recipient} has no key. Cannot send encrypted message")
                return
            # magic byte marker for encrypted messages followed by cipher
            outgoing_text = (
                self.encryption_marker.encode() + Fernet(key).encrypt(text.encode())
            ).decode()
        else:
            outgoing_text = text

        self.get_buf(recipient).insert(
            IrcRawMessage(self.nick, "PRIVMSG", [recipient, text], secure=encrypt)
        )
        await self.send_cmd("PRIVMSG", recipient, outgoing_text)

    async def send_notice(self, recipient: str, text: str) -> None:
        self.get_buf(recipient).insert(IrcRawMessage(self.nick, "NOTICE", [recipient, text]))
        await self.send_cmd("NOTICE", recipient, text)

    async def send_ctcp_request(self, recipient: str, text: str) -> None:
        await self.send_message(recipient, f"\x01{text}\x01")

    async def send_ctcp_reply(self, recipient: str, text: str) -> None:
        await self.send_notice(recipient, f"\x01{text}\x01")

    async def process_privmsg_ctcp(self, message: IrcRawMessage) -> None:
        source = message.prefix_nick
        target = message.params[0]
        text = message.params[1].strip("\x01")

        self.log(f'Received command "{text}" from {source}', "CTCP")

        if text == "VERSION":
            await self.send_ctcp_reply(source, f"VERSION {self.version}")
        elif text.startswith("PING"):
            await self.send_ctcp_reply(source, text)
        elif text.startswith("TIME"):
            await self.send_ctcp_reply(source, f"TIME {datetime.now().isoformat()}")
        elif text.startswith("USERINFO"):
            await self.send_ctcp_reply(source, f"USERINFO {self.nick}")
        elif text.startswith("ACTION"):
            self.get_buf(target).insert(IrcRawMessage(message.prefix, "ACTION", [target, text]))
        elif text.startswith("DCC"):
            try:
                _, send_type, filename, ip, port, size, *args = text.split()
                dcc = DCC(
                    source=source,
                    filename=filename,
                    size=int(size),
                    ip=str(ipaddress.ip_address(int(ip))),
                    port=int(port),
                    start_time=datetime.now(),
                    ssl=send_type == "SSEND",
                )
            except ValueError:
                self.log_error("Malformed CTCP DCC command", "CTCP")
                return
            self.dcc.append(dcc)
            self._delay(self.dcc_download(dcc))
        else:
            self.log_error("Unknown CTCP command", "CTCP")

    async def process_privmsg(self, message: IrcRawMessage) -> None:
        """Process any kind of PRIVMSG. Unwrap potential encryption transparently."""
        assert message.command == "PRIVMSG"

        self.decrypt_privmsg(message)

        target, text = message.params

        if text.startswith("\x01") and text.endswith("\x01"):
            await self.process_privmsg_ctcp(message)
        elif self.nick == target:
            await self.process_user_message(message)
        else:
            await self.process_channel_message(message)

    async def process_channel_message(self, message: IrcRawMessage) -> None:
        """React to channel messages. Ideal for customization."""
        channel, text = message.params
        self.channels[channel].buf.insert(message)

    async def process_user_message(self, message: IrcRawMessage) -> None:
        """React to personal messages. Ideal for customization."""
        _, text = message.params
        self.chats[message.prefix_nick].insert(message)

    def decrypt_privmsg(self, message: IrcRawMessage) -> None:
        _, text = message.params
        source = message.prefix_nick
        # Magic byte to mark message as encrypted; not part of the cipher
        if not text.startswith(self.encryption_marker) or source not in self.keys:
            return

        try:
            cipher = text[len(self.encryption_marker) :]
            message.params[1] = Fernet(key=self.keys[source]).decrypt(cipher, ttl=5).decode()
            message.secure = True
            self.log(message)  # also log decrypted version of message
        except (InvalidToken, KeyError):
            self.log("failed decrypting message", "SEC")
            self.get_buf(source).insert(IrcRawMessage(source, "SEC", ["failed decrypting message"]))

    def dcc_status_callback(self, dcc: DCC) -> None:
        msg = (
            f'Receiving "{dcc.filename}" from {dcc.source}: {dcc.bytes_received / 2**20:.0f} MB - '
            f"{dcc.bytes_received / dcc.size * 100:.1f} % - "
            f"{dcc.bytes_received / (datetime.now() - dcc.start_time).total_seconds() / 2**20:.2f} MB/s"
        )
        self.log(msg, "DCC")
        self.get_buf(dcc.source).insert(IrcRawMessage(dcc.source, "DCC", [msg]))

    async def dcc_complete_callback(self, dcc: DCC) -> None:
        pass

    async def dcc_download(self, dcc: DCC) -> None:
        if not self.dcc_dir:
            self.log_error("DCC directory not set. Doing nothing.", "DCC")
            return

        self.log(f"Opening connection for {dcc} (size: {dcc.size}) ...", "DCC")
        if dcc.ssl:
            ssl_ctx = ssl.SSLContext()
            ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")  # be lenient, this is not banking.
        else:
            ssl_ctx = False  # type: ignore[assignment]
        try:
            reader, writer = await asyncio.open_connection(
                host=dcc.ip, port=dcc.port, limit=2**24, ssl=ssl_ctx
            )
            self.log("Connection established", "DCC")
        except Exception as e:
            self.log(f"Failed opening connection: {e}\n{traceback.format_exc()}\n", "DCC")
            return

        last_status = datetime.now()
        with open(Path(self.dcc_dir) / dcc.filename, "wb") as fh:
            while True:
                data = await reader.read(2**20)
                fh.write(data)
                # housekeeping & progress report
                dcc.bytes_received += len(data)
                now = datetime.now()
                if (now - last_status) > timedelta(seconds=10) or dcc.complete:
                    self.dcc_status_callback(dcc)
                    last_status = now
                if dcc.complete:
                    # Answer with the received byte count as a 64bit long - other end should close
                    writer.write(struct.pack("!Q", dcc.bytes_received) + b"\r\n\r\n")
                    await writer.drain()
                    break

        dcc.end_time = datetime.now()
        await asyncio.sleep(5)  # give peer some time to wrap-up
        writer.close()
        await writer.wait_closed()
        self.log(f"Closed connection for {dcc}", "DCC")
        await self.dcc_complete_callback(dcc)

    @classmethod
    def parse_raw_message(cls, message: bytes) -> IrcRawMessage:
        """Parse an RFC2812-compliant message from the IRC server."""
        tmp = message.decode("utf-8", errors="ignore").strip()
        prefix = None
        trailing: list[str] = []

        if tmp.startswith(":"):
            prefix, tmp = tmp[1:].split(" ", 1)
        if " :" in tmp:
            tmp, tmp_trailing = tmp.split(" :", 1)
            trailing = [tmp_trailing]
        args = tmp.split()
        command = args.pop(0) if args else ""

        return IrcRawMessage(prefix=prefix, command=command, params=args + trailing)

    async def process_message(self, message: IrcRawMessage) -> None:
        # allow to patch in alternate functionality
        if callback := getattr(self, f"on_{message.command.lower()}_callback", None):
            callback(message)
            return

        # default handling
        match message.command:
            case "PING":
                # keep-alive signal from server
                await self.send_cmd("PONG", message.params[0])
            case "PRIVMSG":
                await self.process_privmsg(message)
            case "MODE":
                target, mode_str = message.params[:2]
                if target == self.nick:
                    modifier, *modes = mode_str  # type: tuple[str, list[str]] # type: ignore[misc]
                    for mode in modes:
                        match modifier:
                            case "+":
                                self.mode.add(mode)
                            case "-":
                                self.mode.discard(mode)
                    self.server_buf.insert(message)
                else:
                    self.get_buf(target).insert(message)
            case "NICK":
                if message.prefix == self.nick:
                    self.nick = message.params[0]
                self.server_buf.insert(message)
            case "NOTICE":
                if (
                    not self.mode
                    and message.params[0] == "*"
                    and message.prefix_nick not in self.servers
                ):
                    # This is likely the first message we received from this server
                    self.servers.append(message.prefix_nick)

                # Centralize special service notices
                if message.prefix_nick in ("NickServ", "HostServ", "ChanServ"):
                    self.server_buf.insert(message)
                else:
                    self.get_buf(message.prefix_nick).insert(message)
            case "001" | "002" | "003" | "004" | "005":
                # server details on connect
                self.server_buf.insert(message)
            case "250" | "251" | "252" | "253" | "254" | "255" | "265" | "266":
                # server stats on connect
                self.server_buf.insert(message)
            case "322" | "323":
                # RPL_LIST / RPL_LISTEND - channel listing
                self.server_buf.insert(message)
            case "372" | "375" | "376":
                # MOTD related
                self.server_buf.insert(message)
            case "396":
                # NO OFFICIAL CODE - RPL_HOSTHIDDEN
                self.server_buf.insert(message)
            case "331":
                # RPL_NOTOPIC
                pass
            case "332":
                # RPL_TOPIC
                _, chan_name, topic = message.params
                self.channels[chan_name].topic = topic
            case "333":
                _, chan_name, topic_author, topic_time = message.params
                self.channels[chan_name].topic_origin = (topic_author, topic_time)
            case "353":
                # RPL_NAMREPLY
                chan_name = message.params[2]
                for user in message.params[3].split():
                    self.channels[chan_name].users.add(user.lstrip("+&"))
                self.channels[chan_name].buf.insert(message)
            case "366":
                # RPL_ENDOFNAMES
                pass
            case "401":
                # ERR_NOSUCHNICK
                self.server_buf.insert(message)
            case "403":
                # ERR_NOSUCHCHANNEL
                self.server_buf.insert(message)
            case "404":
                # ERR_CANNOTSENDTOCHAN
                self.server_buf.insert(message)
            case "442":
                # ERR_NOTONCHANNEL
                self.get_buf(message.params[1]).insert(message)
            case "900":
                # NO OFFICIAL CODE - Logged-in notification
                self.server_buf.insert(message)
            case "JOIN":
                chan_name = message.params[0]
                self.channels[chan_name].users.add(message.prefix_nick)
                self.channels[chan_name].buf.insert(message)
            case "PART":
                chan_name = message.params[0]
                self.channels[chan_name].buf.insert(message)
            case "QUIT":
                for channel in self.channels.values():
                    if message.prefix_nick in channel.users:
                        channel.users.discard(message.prefix_nick)
                        channel.buf.insert(message)
            case "KICK":
                chan_name, nick = message.params[:2]
                self.channels[chan_name].users.discard(nick)
                self.channels[chan_name].buf.insert(message)
            case "ERROR":
                self.server_buf.insert(message)
            case _:
                self.log_error(message, "Unknown command")
                self.server_buf.insert(message)

    def log_error(self, message: IrcRawMessage | str, head: str | None = None) -> None:
        self.log(message, head, level=logging.ERROR)

    def log(
        self,
        message: IrcRawMessage | str,
        head: str | None = None,
        level: int = logging.INFO,
    ) -> None:
        msg = (
            f"{message.ts if isinstance(message, IrcRawMessage) else datetime.now()}:{self.host}"
            f":{head or (message.command if isinstance(message, IrcRawMessage) else 'N/A')}"
            f":{message}"
        )
        if not isinstance(message, IrcRawMessage):
            self.log_buf.insert(IrcRawMessage(head or "INT", str(level), [message]))
        match self.log_mode:
            case "file":
                self._fh.write(msg + "\n")
                self._fh.flush()
            case "console":
                logger.log(level, msg)

    def delay(self, coro: Coroutine[Any, Any, None], loop: asyncio.AbstractEventLoop) -> None:
        """External forking of tasks"""
        self._futures.add(asyncio.run_coroutine_threadsafe(coro, loop))

    def _delay(self, coro: Coroutine[Any, Any, None]) -> None:
        """Internal forking of tasks"""
        self._futures.add(asyncio.create_task(coro))

    async def _connection_loop(self) -> None:
        """Listen for and handle incoming messages from the server."""
        try:
            await self._connect()
            while True:
                try:
                    response = await asyncio.wait_for(self._reader.readline(), timeout=5 * 60)
                    if not response and self._reader.at_eof():
                        raise ServerTerminationError()
                except TimeoutError:
                    self.log("Timeout occurred. Probing connection ...")
                    # Attempt to send something to provoke ConnectionResetError being raised in case
                    # the connection was lost. Apparently cannot detect staleness any other way.
                    await self.send_cmd("TIME")
                    continue
                message = self.parse_raw_message(response)
                self.log(message, "IN")
                await self.process_message(message)
        except ConnectionResetError:
            self.log_error("Connection reset. Reconnecting ...")
        except OSError as e:
            if e.errno == 60:
                self.log_error("Operation timed out. Reconnecting ...")
            elif e.errno == 65:
                self.log_error("No route to host. Reconnecting ...")
            else:
                raise

    async def run(self) -> None:
        """
        Main entry point for the IRC Client. Handles disconnects, timeouts and cleanup.
        Only returns on unexpected errors or the server actively closing the connection.
        """
        try:
            while True:
                self.log(f"Connecting to {self.host}:{self.port} ...")
                await self._connection_loop()
                await asyncio.sleep(5)
        except asyncio.CancelledError as e:
            self.log(f"CancelledError: {e}\n{traceback.format_exc()}\n", "ERROR")
        except ServerTerminationError:
            self.log("Server terminated the connection", "TERM")
        except Exception as e:
            self.log_error(f"Generic Exception: {e}\n{traceback.format_exc()}", "ERROR")
        finally:
            self.log("Closing open connection and files ...")
            if self.log_mode == "file":
                self._fh.close()
            self._writer.close()
            await self._writer.wait_closed()
