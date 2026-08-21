import asyncio
import base64
import contextlib
import errno
import ipaddress
import logging
import random
import struct
import traceback
from asyncio import StreamReader, StreamWriter, Task
from collections import defaultdict
from collections.abc import Callable, Coroutine, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import UNIQUE, StrEnum, verify
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from tomlkit.exceptions import TOMLKitError

from kirk import ASCII_LOGO, VERSION
from kirk.security import KeyExchange, build_client_ssl_context, build_server_ssl_context
from kirk.utils import SPECIAL_USERS, persist_key

logger = logging.getLogger("IrcClient")


class ServerTerminationError(Exception):
    pass


@verify(UNIQUE)
class ChannelUserPerm(StrEnum):
    OWNER = "q"
    ADMIN = "a"
    OPERATOR = "o"
    HALF_OP = "h"
    VOICE = "v"
    BASE = ""

    def __lt__(self, other: "ChannelUserPerm") -> bool:  # type: ignore[override]
        order = {p: o for o, p in enumerate(ChannelUserPerm)}
        return order[self] < order[other]


CHANNEL_PERM_MAPPING: dict[ChannelUserPerm, str] = {
    ChannelUserPerm.OWNER: "~",
    ChannelUserPerm.ADMIN: "&",
    ChannelUserPerm.OPERATOR: "@",
    ChannelUserPerm.HALF_OP: "%",
    ChannelUserPerm.VOICE: "+",
    ChannelUserPerm.BASE: "",
}
CHANNEL_PERM_MAPPING_REV: dict[str, ChannelUserPerm] = {v: k for k, v in CHANNEL_PERM_MAPPING.items() if k}


@verify(UNIQUE)
class Marker(StrEnum):
    AUTHENTICATE_COMMENCED = "AUTHENTICATE+"
    CAP_LS_DONE = "CAP-LS-DONE"
    CAP_ACK_DONE = "CAP-ACK-DONE"
    SASL_DONE = "SASL-DONE"
    AUTHENTICATED = "AUTHENTICATED"


@dataclass
class IrcRawMessage:
    """Represents a parsed IRC message with metadata."""

    prefix: str | None
    command: str
    params: list[str]
    tags: dict[str, str | None] | None = None
    secure: bool = False
    ts: datetime = field(default_factory=datetime.now)

    @property
    def prefix_nick(self) -> str:
        """Extract nickname from IRC message prefix."""
        if self.prefix and "!" in self.prefix:
            return self.prefix.split("!")[0]
        return self.prefix or ""

    @property
    def prefix_host(self) -> str:
        """Extract host/user info from IRC message prefix."""
        if self.prefix and "!" in self.prefix:
            return self.prefix.split("!")[1]
        return ""


class Buffer[T]:
    """Fixed-length circular buffer"""

    def __init__(self, size: int | None = None):
        self.size = size or 4096
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
        """Iterate buffer LIFO (newest first)."""
        if self.len < self.size:
            yield from reversed(self._buf)
        else:
            yield from reversed(self._buf[: self.idx])
            yield from reversed(self._buf[self.idx :])

    def __reversed__(self) -> Iterator[T]:
        if self.len < self.size:
            yield from self._buf
        else:
            yield from self._buf[self.idx :]
            yield from self._buf[: self.idx]

    def fixed_iter(self, start: int) -> Iterator[T]:
        """Iterate from fixed start point for scrollback functionality."""
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
    """Represents an IRC channel with message buffer and metadata."""

    def __init__(self, name: str, buf_size: int | None = None):
        self.buf = Buffer[IrcRawMessage](buf_size)
        self.name = name
        self.mode = ""
        self.topic = ""
        self.topic_origin: tuple[str, str] = ("", "")
        self.users: dict[str, set[ChannelUserPerm]] = {}


class ChannelDict(defaultdict[str, IrcChannel]):
    """Auto-creating dictionary for IRC channels."""

    def __missing__(self, key: str) -> IrcChannel:
        """Create new channel when accessed."""
        self[key] = value = IrcChannel(key)
        return value


@dataclass
class DCC:
    """Represents a DCC file transfer session."""

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


def is_ctcp(text: str) -> bool:
    return text.startswith("\x01") and text.endswith("\x01")


class IrcClient:
    """
    Basic IRC Client based on
        - https://datatracker.ietf.org/doc/html/rfc2812#section-3.7.3
        - https://datatracker.ietf.org/doc/html/draft-oakley-irc-ctcp-02
        - https://www.alien.net.au/irc/irc2numerics.html
        - https://modern.ircdocs.horse/formatting
    """

    version = f"Kirk {VERSION} (python)"
    encryption_marker = "~"
    cap_client = {"message-tags", "sasl"}

    def __init__(
        self,
        host: str,
        nick: str,
        auto_join: Sequence[str] | None = None,
        auth: Literal["nickserv", "sasl_plain"] = "sasl_plain",
        password: str | None = None,
        port: int | None = None,
        ssl: bool = True,
        keys: dict[str, str] | None = None,
        dcc_dir: str | None = None,
        dcc_host_ip: str | None = None,
        dcc_port_range: tuple[int, int] = (10_000, 11_000),
        log_mode: Literal["file", "console", "none"] = "none",
        config_path: str | None = None,
    ):
        self.host = host
        self.port = port or (6697 if ssl else 6667)
        self.nick = nick
        self.server_aliases: list[str] = []
        self.cap_server: dict[str, str | None] = {}
        self.cap_enabled: set[str] = set()
        self.mode: set[str] = set()
        self.auto_join = auto_join or []
        self.auth = auth
        self.password = password
        self.ssl = ssl
        self.dcc_dir = dcc_dir
        self.dcc_host_ip = dcc_host_ip or "127.0.0.1"
        self.dcc_port_range = dcc_port_range
        self.keys: dict[str, str] = keys or {}
        self.channels = ChannelDict()
        self.chats = defaultdict[str, Buffer[IrcRawMessage]](Buffer)
        self.dcc: list[DCC] = []
        self.log_mode = log_mode
        self.config_path = config_path
        self._reader: StreamReader = None  # type: ignore[assignment]
        self._writer: StreamWriter = None  # type: ignore[assignment]
        self._futures = set[Future[Any] | Task[Any]]()
        self._attention: bool = False
        self._marker: set[str] = set()
        self._dh_pending: dict[str, KeyExchange] = {}
        if log_mode == "file":
            self._fh = open(f"kirk_{datetime.now().isoformat()}_{random.randint(100, 999)}.log", "w")  # noqa: SIM115
        for ll in ASCII_LOGO.split("\n"):
            self.log(ll)

    @property
    def server_buf(self) -> Buffer[IrcRawMessage]:
        return self.chats[self.server_buf_name]

    @property
    def server_buf_name(self) -> str:
        return self.host

    @property
    def attention_requested(self) -> bool:
        result, self._attention = self._attention, False
        return result

    def get_buf(self, name: str) -> Buffer[IrcRawMessage]:
        if name == self.host or name in self.server_aliases:
            return self.server_buf
        elif self.is_channel_name(name):
            return self.channels[name].buf
        else:
            return self.chats[name]

    async def delete(self, name: str) -> None:
        """Remove channel/chat from client state"""
        if name == self.host or name in self.server_aliases:
            pass
        elif self.is_channel_name(name):
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
        # request server capabilities and introduce ourselves
        await self.send_cmd("CAP", ["LS", "302"])
        await self.change_nick(self.nick)
        await self.user_introduction()
        # we need server replies now - fork of next steps and go into main loop
        self._delay(self._post_connect())

    async def _post_connect(self) -> None:
        # wait for full list of capabilities and request what we and the server both support
        await self.wait_for_marker(Marker.CAP_LS_DONE)
        await self.send_cmd("CAP", "REQ", " ".join(set(self.cap_server) & self.cap_client))
        await self.wait_for_marker(Marker.CAP_ACK_DONE)

        if self.password and self.auth == "sasl_plain":
            await self.perform_sasl_auth()

        await self.send_cmd("CAP", "END")

        if self.password and self.auth == "nickserv":
            await self.perform_nickserv_auth()

        if self.auto_join:
            await self.perform_auto_join()

    async def perform_sasl_auth(self) -> None:
        await self.send_cmd("AUTHENTICATE", "PLAIN")
        await self.wait_for_marker(Marker.AUTHENTICATE_COMMENCED)

        payload = base64.b64encode(f"\0{self.nick}\0{self.password}".encode()).decode()
        await self.send_cmd("AUTHENTICATE", payload)
        await self.wait_for_marker(Marker.SASL_DONE)

    async def perform_nickserv_auth(self) -> None:
        # wait for establishing boilerplate to end. mode from server is usually the end
        await self.wait_for(lambda: self.mode)
        await self.send_message("NickServ", f"IDENTIFY {self.nick} {self.password}")

    async def perform_auto_join(self) -> None:
        # wait to be onboarded and logged-in (if credentials given)
        await self.wait_for(
            lambda: self.mode and (not self.password or Marker.AUTHENTICATED in self._marker),
            timeout=999,
        )
        for channel in self.auto_join:
            await self.join_channel(channel)

    async def _send_raw(self, raw: str) -> None:
        self.log(raw, "OUT", show=False)
        self._writer.write((raw + "\r\n").encode("utf-8"))
        await self._writer.drain()

    async def send_cmd(
        self,
        command: str,
        params: str | list[str] = "",
        trailing: str = "",
        tags: dict[str, str | None] | None = None,
    ) -> None:
        tags_str = f"@{';'.join(f'{k}={v}' if v else k for k, v in tags.items())} " if tags else ""
        if isinstance(params, list):
            params = " ".join(params)
        params = f" {params}" if params else ""
        trailing = f" :{trailing}" if trailing else ""
        await self._send_raw(f"{tags_str}{command}{params}{trailing}")

    async def join_channel(self, channel: str, password: str | None = None) -> None:
        await self.send_cmd("JOIN", channel)

    async def part_channel(self, channel: str, reason: str = "") -> None:
        await self.send_cmd("PART", channel, reason)

    async def user_introduction(self, realname: str | None = None) -> None:
        await self.send_cmd("USER", [self.nick, "0", "*"], realname or self.nick)

    async def change_nick(self, nick: str) -> None:
        await self.send_cmd("NICK", nick)

    async def quit(self, text: str = "") -> None:
        await self.send_cmd("QUIT", [], text)

    async def list(self) -> None:
        await self.send_cmd("LIST")

    async def whois(self, nick: str) -> None:
        await self.send_cmd("WHOIS", nick)

    async def whowas(self, nick: str) -> None:
        await self.send_cmd("WHOWAS", nick)

    async def who(self, nick: str) -> None:
        await self.send_cmd("WHO", nick)

    async def send_message(self, recipient: str, text: str, encrypt: bool | None = None) -> None:
        """Try to send message encrypted if we have a key and NOT specifically disabled"""
        key = self.keys.get(recipient)
        if not key and encrypt:
            self.log_error(f"{recipient} has no key. Cannot send encrypted message")
            return
        if key and encrypt is not False:
            # magic byte marker for encrypted messages followed by cipher
            outgoing_text = (self.encryption_marker.encode() + Fernet(key).encrypt(text.encode())).decode()
            secure = True
        else:
            outgoing_text = text
            secure = False

        self.get_buf(recipient).insert(IrcRawMessage(self.nick, "PRIVMSG", [recipient, text], secure=secure))
        await self.send_cmd("PRIVMSG", recipient, outgoing_text)

    async def send_notice(self, recipient: str, text: str) -> None:
        self.get_buf(recipient).insert(IrcRawMessage(self.nick, "NOTICE", [recipient, text]))
        await self.send_cmd("NOTICE", recipient, text)

    async def send_tagmsg(self, target: str, tags: dict[str, str | None]) -> None:
        await self.send_cmd("TAGMSG", target, tags=tags)

    async def send_ctcp_request(self, recipient: str, text: str) -> None:
        await self.send_message(recipient, f"\x01{text}\x01")

    async def send_ctcp_reply(self, recipient: str, text: str) -> None:
        await self.send_notice(recipient, f"\x01{text}\x01")

    async def start_key_exchange(self, recipient: str) -> None:
        """Kick off a DH handshake to establish a shared encryption key with `recipient`."""
        if recipient in self._dh_pending or not self._dh_handshake_allowed(recipient):
            return
        self._dh_pending[recipient] = dh = KeyExchange()
        await self.send_ctcp_request(recipient, f"DH1 {dh.public_key} {dh.salt}")

    async def process_ctcp_dh1(self, source: str, text: str) -> None:
        self.log(f"Incoming secure channel handshake request from {source}", "SEC", source)
        if not self._dh_handshake_allowed(source, tampering=True):
            return
        try:
            _, peer_public_key, salt = text.split()
            dh = KeyExchange(peer_public_key, salt)
        except ValueError:
            self.log_error("Malformed DH handshake request", "SEC", source)
        else:
            await self.send_ctcp_request(source, f"DH2 {dh.public_key}")
            self._complete_secure_channel(source, dh)

    async def process_ctcp_dh2(self, source: str, text: str) -> None:
        dh = self._dh_pending.pop(source, None)
        if not dh:
            self.log_error("Unexpected DH handshake reply", "SEC", source)
            return
        if not self._dh_handshake_allowed(source, tampering=True):
            return
        self.log(f"Received handshake reply from {source}, finalizing secure channel", "SEC", source)
        try:
            _, peer_public_key = text.split()
            dh.set_peer_public_key(peer_public_key)
        except ValueError:
            self.log_error("Malformed DH handshake reply", "SEC", source)
        else:
            self._complete_secure_channel(source, dh)

    def _dh_handshake_allowed(self, source: str, tampering: bool = False) -> bool:
        if source not in self.keys:
            return True
        note = " (possible tampering)" if tampering else ""
        self.log_error(f"Refusing DH handshake with {source}: a key already exists{note}", "SEC", source)
        return False

    def _complete_secure_channel(self, source: str, dh: KeyExchange) -> None:
        self.log(f"Secure channel established. Verify fingerprint: {dh.fingerprint}", "SEC", source)
        self.keys[source] = dh.shared_key

        if self.config_path:
            try:
                persist_key(self.config_path, self.host, source, dh.shared_key)
            except (OSError, TOMLKitError, KeyError) as e:
                self.log_error(f"Could not persist key to {self.config_path}: {e}", "SEC")

    async def process_privmsg_ctcp(self, message: IrcRawMessage) -> None:
        source = message.prefix_nick
        target = message.params[0]
        text = message.params[1].strip("\x01")

        if not text.startswith("ACTION"):
            self.get_buf(source).insert(message)

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
        elif text.startswith("DH1"):
            await self.process_ctcp_dh1(source, text)  # kirk encryption feature
        elif text.startswith("DH2"):
            await self.process_ctcp_dh2(source, text)  # kirk encryption feature
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
                self.log_error("Malformed CTCP DCC command", "CTCP", source)
            else:
                self.dcc.append(dcc)
                self._delay(self.dcc_download(dcc))
        else:
            self.log_error("Unknown CTCP command", "CTCP", source)

    async def process_privmsg(self, message: IrcRawMessage) -> None:
        """Process any kind of PRIVMSG. Unwrap potential encryption transparently."""
        assert message.command == "PRIVMSG"

        self.decrypt_privmsg(message)

        target, text = message.params

        if is_ctcp(text):
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
            self.log(message, "IN", show=False)
        except (InvalidToken, KeyError):
            self.log_error("failed decrypting message", "SEC", source)

    def dcc_status_callback(self, dcc: DCC) -> None:
        msg = (
            f'Receiving "{dcc.filename}" - {dcc.bytes_received / 2**20:.0f} MB - '
            f"{dcc.bytes_received / dcc.size * 100:.1f} % - "
            f"{dcc.bytes_received / (datetime.now() - dcc.start_time).total_seconds() / 2**20:.2f} MB/s"
        )
        self.log(msg, "DCC", dcc.source)

    async def dcc_complete_callback(self, dcc: DCC) -> None:
        pass

    async def dcc_download(self, dcc: DCC) -> None:
        if not self.dcc_dir:
            self.log_error("DCC directory not set. Doing nothing.", "DCC", dcc.source)
            return
        try:
            reader, writer = await asyncio.open_connection(
                host=dcc.ip, port=dcc.port, limit=2**24, ssl=build_client_ssl_context() if dcc.ssl else None
            )
            self.log(
                f"{'Secure c' if dcc.ssl else 'C'}onnection established to {dcc.ip}:{dcc.port}",
                "DCC",
                dcc.source,
            )
        except Exception as e:
            self.log(f"Failed opening connection: {e}\n{traceback.format_exc()}\n", "DCC", dcc.source)
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
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        self.log("Connection closed. File written successfully", "DCC", dcc.source)
        await self.dcc_complete_callback(dcc)

    async def dcc_send(self, recipient: str, file: Path, ssl: bool = False) -> None:
        """Offer `file` to `recipient` over DCC and serve it once they accept."""

        async def handle_request(reader: StreamReader, writer: StreamWriter) -> None:
            self.log(f'Connection established. Sending "{file.name}"', "DCC", recipient)
            # stop accepting further connections immediately, before serving this one
            server.close()
            try:
                with open(file, "rb") as fh:
                    await asyncio.get_event_loop().sendfile(writer.transport, fh)
            finally:
                # always close, even on a failed transfer, so wait_closed() doesn't hang forever
                writer.close()
                await writer.wait_closed()

        if not self.dcc_host_ip:
            self.log_error("dcc_host_ip not set. Cannot offer file.", "DCC", recipient)
            return
        if not file.is_file():
            self.log_error(f"No such file: {file}", "DCC", recipient)
            return

        host = int(ipaddress.ip_address(self.dcc_host_ip))
        port = random.randint(*self.dcc_port_range)

        # backlog=1 so the OS refuses any further connection attempts at the TCP
        # level while our single accepted connection is being served
        self.log(f"Starting server on port {port}", "DCC", recipient)
        server = await asyncio.start_server(
            handle_request, "0.0.0.0", port, backlog=1, ssl=build_server_ssl_context() if ssl else None
        )
        # advertise the offering with connection details to recipient
        send_type = "SSEND" if ssl else "SEND"
        await self.send_ctcp_request(
            recipient, f"DCC {send_type} {file.name} {host} {port} {file.stat().st_size}"
        )
        async with server:
            await server.wait_closed()

        self.log("Connection closed", "DCC", recipient)

    @classmethod
    def parse_raw_message(cls, message: bytes) -> IrcRawMessage:
        """Parse an RFC2812-compliant message from the IRC server."""
        tmp = message.decode("utf-8", errors="ignore").strip()
        prefix = None
        tags = None
        trailing: list[str] = []

        if tmp.startswith("@"):
            raw_tags, tmp = tmp[1:].split(" ", 1)
            tags = cls.explode_dictstr(raw_tags, ";")
        if tmp.startswith(":"):
            prefix, tmp = tmp[1:].split(" ", 1)
        if " :" in tmp:
            tmp, tmp_trailing = tmp.split(" :", 1)
            trailing = [tmp_trailing]
        args = tmp.split()
        command = args.pop(0) if args else ""

        return IrcRawMessage(prefix=prefix, command=command, params=args + trailing, tags=tags)

    @classmethod
    def process_mode_str(cls, mode_str: str) -> Iterator[tuple[bool, str]]:
        for mode in mode_str[1:]:
            match mode_str[0]:
                case "+":
                    yield True, mode
                case "-":
                    yield False, mode

    @classmethod
    def explode_dictstr(cls, tagstr: str, div: str | None, sep: str = "=") -> dict[str, str | None]:
        result = {}
        for tag in tagstr.split(div):
            key, _sep, val = tag.partition(sep)
            result[key] = val if _sep else None
        return result

    @classmethod
    async def wait_for(cls, condition: Callable[[], Any], timeout: int = 15) -> bool:
        for _ in range(timeout * 10):
            if condition():
                return True
            await asyncio.sleep(0.1)
        return False

    async def wait_for_marker(self, marker: Marker, clear: bool = True, timeout: int = 15) -> bool:
        result = await self.wait_for(lambda: marker in self._marker, timeout=timeout)
        if result and clear:
            self._marker.discard(marker)
        return result

    @classmethod
    def is_channel_name(cls, name: str) -> bool:
        return any(name.startswith(prefix) for prefix in "&#+!")

    async def process_message(self, message: IrcRawMessage) -> None:
        """Handle incoming messages from server"""
        match message.command:
            case "PING":
                # keep-alive signal from server
                await self.send_cmd("PONG", message.params[0])
            case "PRIVMSG":
                await self.process_privmsg(message)
            case "MODE":
                if len(message.params) == 2:
                    # personal user mode or channel mode
                    user_or_channel, mode_str = message.params
                    for add, mode in self.process_mode_str(mode_str):
                        if add:
                            self.mode.add(mode)
                        else:
                            self.mode.discard(mode)

                    if self.nick == user_or_channel:
                        self.server_buf.insert(message)
                    else:
                        self.channels[user_or_channel].buf.insert(message)
                elif len(message.params) == 3:
                    # user channel mode
                    chan, mode_str, user = message.params
                    for add, mode in self.process_mode_str(mode_str):
                        if mode == "b":
                            pass  # ignore bans; also mapping a mask is tricky
                        elif user not in self.channels[chan].users or mode not in ChannelUserPerm:
                            self.log_error(f"MODE set failure for {mode_str} on user {user} in {chan}")
                        elif add:
                            self.channels[chan].users[user].add(ChannelUserPerm(mode))
                        else:
                            self.channels[chan].users[user].discard(ChannelUserPerm(mode))

                    self.get_buf(chan).insert(message)
                else:
                    self.log_error(f"MODE message unexpected {message.params}")
            case "TAGMSG":
                _target = message.params[0]
                # self.get_buf(target).insert(message)  # TODO
            case "CAP":
                # Server         with 302: CAP * LS * :cap-notify server-time example.org/dummy-cap=dummy
                # Server with/without 302: CAP * LS :userhost-in-names sasl=EXTERNAL,DH-AES,PLAIN
                _, subcommand, *cap_params = message.params
                match subcommand:
                    case "LS":
                        self.cap_server.update(self.explode_dictstr(cap_params[-1], None))
                        if cap_params[0] != "*":
                            self._marker.add(Marker.CAP_LS_DONE)
                    case "ACK":
                        self.cap_enabled.update(cap_params[-1].split())
                        self._marker.add(Marker.CAP_ACK_DONE)
                self.server_buf.insert(message)
            case "AUTHENTICATE":
                if message.params and message.params[0] == "+":
                    self._marker.add(Marker.AUTHENTICATE_COMMENCED)
            case "NICK":
                new_nick = message.params[0]
                if message.prefix_nick == self.nick:
                    self.nick = new_nick
                # update users in channels
                for channel in self.channels.values():
                    if message.prefix_nick in channel.users:
                        channel.users[new_nick] = channel.users.pop(message.prefix_nick)
                        channel.buf.insert(message)
            case "NOTICE":
                if (
                    message.params[0] == "*"
                    and message.prefix
                    and message.prefix_nick not in self.server_aliases
                ):
                    # This is likely the first message we received from this server
                    self.server_aliases.append(message.prefix_nick)

                # Centralize special service notices
                if not message.prefix or message.prefix_nick in SPECIAL_USERS:
                    self.server_buf.insert(message)
                else:
                    self.get_buf(message.prefix_nick).insert(message)
            case "001" | "002" | "003" | "004" | "005":
                # RPL_WELCOME | RPL_YOURHOST | RPL_CREATED | RPL_MYINFO | RPL_BOUNCE
                # server details on connect - make sure origin prefix is marked as server
                if (
                    message.command == "001"
                    and message.prefix
                    and message.prefix_nick not in self.server_aliases
                ):
                    self.server_aliases.append(message.prefix_nick)

                self.server_buf.insert(message)
            case "221":
                # RPL_UMODEIS - user mode string
                self.server_buf.insert(message)
            case "250" | "251" | "252" | "253" | "254" | "255" | "265" | "266":
                # RPL_STATSCONN | RPL_LUSERCLIENT | RPL_LUSEROP | RPL_LUSERUNKNOWN |
                # RPL_LUSERCHANNELS | RPL_LUSERME | RPL_LOCALUSERS | RPL_GLOBALUSERS
                # server stats on connect
                self.server_buf.insert(message)
            case "305" | "306":
                # RPL_UNAWAY | RPL_NOWAWAY
                self.server_buf.insert(message)
            case "322" | "323":
                # RPL_LIST / RPL_LISTEND - channel listing
                self.server_buf.insert(message)
            case "372" | "375" | "376":
                # MOTD related
                self.server_buf.insert(message)
            case "391":
                # RPL_TIME
                self.server_buf.insert(message)
            case "396":
                # RPL_HOSTHIDDEN
                self.server_buf.insert(message)
            case "311" | "312" | "313" | "314" | "315" | "316" | "317" | "318" | "369" | "671":
                # RPL_WHOISUSER | RPL_WHOISSERVER | RPL_WHOISOPERATOR | RPL_WHOWASUSER | RPL_ENDOFWHO
                # RPL_WHOISCHANOP | RPL_WHOISIDLE | RPL_ENDOFWHOIS | RPL_ENDOFWHOWAS | RPL_WHOISSECURE
                self.server_buf.insert(message)
            case "331":
                # RPL_NOTOPIC
                pass
            case "332":
                # RPL_TOPIC
                _, chan_name, topic = message.params
                self.channels[chan_name].topic = topic
            case "333":
                # RPL_TOPICWHOTIME
                _, chan_name, topic_author, topic_time = message.params
                self.channels[chan_name].topic_origin = (topic_author, topic_time)
            case "353":
                # RPL_NAMREPLY
                # Example: 353 Peter @ #chan +Daniel Jack Dorothy
                _, _chan_mode, chan_name, users = message.params

                for user in users.split():
                    # capture user prefixes like + & ~ and store user with mapped permission in registry
                    if user.startswith(tuple(CHANNEL_PERM_MAPPING_REV.keys())):
                        self.channels[chan_name].users[user[1:]] = {CHANNEL_PERM_MAPPING_REV[user[0]]}
                    else:
                        self.channels[chan_name].users[user] = set()

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
                # RPL_LOGGEDIN
                self._marker.add(Marker.AUTHENTICATED)
                self.server_buf.insert(message)
            case "901":
                # RPL_LOGGEDOUT
                self._marker.discard(Marker.AUTHENTICATED)
                self.server_buf.insert(message)
            case "903" | "904" | "905":
                # RPL_SASLSUCCESS | ERR_SASLFAIL | ERR_SASLTOOLONG
                self._marker.add(Marker.SASL_DONE)
                self.server_buf.insert(message)
            case "JOIN":
                chan_name = message.params[0]
                self.channels[chan_name].users[message.prefix_nick] = set()
                self.channels[chan_name].buf.insert(message)
            case "PART":
                chan_name = message.params[0]
                self.channels[chan_name].users.pop(message.prefix_nick, None)
                self.channels[chan_name].buf.insert(message)
            case "QUIT":
                for channel in self.channels.values():
                    if message.prefix_nick in channel.users:
                        del channel.users[message.prefix_nick]
                        channel.buf.insert(message)
            case "KICK":
                chan_name, nick = message.params[:2]
                self.channels[chan_name].users.pop(nick, None)
                self.channels[chan_name].buf.insert(message)
            case "ERROR":
                self.server_buf.insert(message)
            case _:
                self.log_error(message, "Unknown command")
                self.server_buf.insert(message)

    def log_error(
        self,
        message: IrcRawMessage | str,
        category: str | None = None,
        source: str | None = None,
    ) -> None:
        self.log(message, category, source=source, level=logging.ERROR)

    def log(
        self,
        message: IrcRawMessage | str,
        category: str | None = None,
        source: str | None = None,
        show: bool = True,
        level: int = logging.INFO,
    ) -> None:
        if isinstance(message, IrcRawMessage):
            msg = f"{message.ts}:{self.host}:{category or message.command}:{message}"
            if show:
                self.get_buf(message.prefix_nick).insert(message)
        else:
            msg = f"{datetime.now()}:{self.host}:{category or '-'}:{message}"
            if show:
                self.get_buf(source or self.host).insert(IrcRawMessage(None, category or "-", [message]))

        match self.log_mode:
            case "file":
                self._fh.write(msg + "\n")
                self._fh.flush()
            case "console":
                logger.log(level, msg)

    def delay(self, coro: Coroutine[Any, Any, None], loop: asyncio.AbstractEventLoop) -> None:
        """External forking of tasks"""
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        self._futures.add(future)
        future.add_done_callback(self._futures.discard)

    def _delay(self, coro: Coroutine[Any, Any, None]) -> None:
        """Internal forking of tasks"""
        task = asyncio.create_task(coro)
        self._futures.add(task)
        task.add_done_callback(self._futures.discard)

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
                self.log(message, "IN", show=False)
                await self.process_message(message)
        except ConnectionResetError:
            self.log_error("Connection reset. Reconnecting ...")
        except OSError as e:
            if e.errno == errno.ETIMEDOUT:
                self.log_error("Operation timed out. Reconnecting ...")
            elif e.errno == errno.EHOSTUNREACH:
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
        except ServerTerminationError:
            self.log("Server terminated the connection", "TERM")
        except Exception as e:
            self.log_error(f"Generic Exception: {e}\n{traceback.format_exc()}", "ERROR")
        finally:
            self.log("Closing open connection and files ...")
            if self.log_mode == "file":
                self._fh.close()
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
