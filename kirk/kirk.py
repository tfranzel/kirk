import curses
import traceback
from asyncio import AbstractEventLoop
from collections.abc import Sequence
from dataclasses import dataclass

from blessed import Terminal
from blessed.keyboard import Keystroke

from kirk.client import DCC, Buffer, IrcClient, IrcRawMessage, is_channel_name
from kirk.color import irc_to_ansi, name_to_rgb
from kirk.transporter import Transporter

OPT_NUMBER_MAPPING = dict(zip("¡™£¢∞§¶•ªº", range(10), strict=False))


class ExitInterrupt(Exception):
    pass


@dataclass
class Window:
    """Stateful scrollable window tab"""

    name: str
    buf: Buffer[IrcRawMessage]
    buf_idx_viewed: int = 0
    buf_idx_frozen: int | None = None
    page: int = 0
    header: str = ""
    dirty_buf_before = False

    def page_up(self) -> None:
        if self.page == 0:
            # freeze buffer view on scroll start
            self.buf_idx_frozen = self.buf_idx_viewed
        self.page += 1

    def page_down(self) -> None:
        self.page = max(0, self.page - 1)
        if self.page == 0:
            # release buffer fix on scroll end
            self.buf_idx_frozen = None

    def page_reset(self) -> None:
        self.page = 0
        self.buf_idx_frozen = None

    def get_buf_page(self, size: int) -> list[IrcRawMessage]:
        if self.buf_idx_frozen is not None:
            buf_view = self.buf.fixed_iter(self.buf_idx_frozen)
        else:
            buf_view = iter(self.buf)
        scroll_off = int(size * 0.66)
        return list(buf_view)[self.page * scroll_off : self.page * scroll_off + size]

    @property
    def dirty_view(self) -> bool:
        return self.page == 0 and self.dirty_buf

    @property
    def dirty_buf(self) -> bool:
        return self.buf_idx_viewed != self.buf.idx

    def reset_buf(self) -> None:
        self.buf_idx_viewed = self.buf.idx


class Kirk:
    """Simple UI for Basic IRC Client"""

    def __init__(self, clients: Sequence[IrcClient], loop: AbstractEventLoop):
        self.t = Terminal()
        self.loop = loop
        self.clients = list(clients)
        self.client_idx = 0
        self.client_windows: list[dict[str, Window]] = [{} for _ in clients]
        self.prompt_buf: list[str] = []
        self.current_window_name: str = self.client.server_buf_name
        self.dirty = True
        self.error_msg = ""
        self._frame = 0

    def switch_window_relative(self, offset: int) -> None:
        window_mapping = dict(enumerate(self.windows.keys()))
        current_idx = list(self.windows.keys()).index(self.current_window_name)

        next_idx = (current_idx + offset) % len(self.windows)

        self.current_window_name = window_mapping[next_idx]
        self.dirty = True

    @property
    def client(self) -> IrcClient:
        return self.clients[self.client_idx]

    @property
    def windows(self) -> dict[str, Window]:
        return self.client_windows[self.client_idx]

    @property
    def current_window(self) -> Window:
        return self.windows[self.current_window_name]

    @property
    def is_server_window(self) -> bool:
        return self.current_window_name == self.client.server_buf_name

    def switch_client(self) -> None:
        """Switch to next client. Completely clear & rebuild the UI state"""
        self.client_idx = (self.client_idx + 1) % len(self.clients)
        self.current_window_name = self.client.server_buf_name
        self.sync_windows()

    def process_input(self, val: Keystroke) -> None:
        if val.code == curses.KEY_PPAGE:
            # scrolling up
            self.current_window.page_up()
            self.dirty = True
        elif val.code == curses.KEY_NPAGE:
            # scrolling down
            self.current_window.page_down()
            self.dirty = True
        elif val.code in [curses.KEY_END, curses.KEY_SELECT]:
            # scrolling down all the way
            self.current_window.page_reset()
            self.dirty = True
        elif val.code in [curses.KEY_LEFT, curses.KEY_RIGHT]:
            # change tabs
            self.switch_window_relative(offset=1 if val.code == curses.KEY_RIGHT else -1)
        elif val.name == "KEY_TAB":
            pass  # TODO
        elif val.name == "KEY_ESCAPE":
            self.error_msg = ""
            self.dirty = True
        elif val.code == curses.KEY_BACKSPACE:
            # prompt delete char
            if self.prompt_buf:
                self.prompt_buf.pop()
            self.dirty = True
            self.error_msg = ""
        elif val.name == "KEY_DELETE":
            # wipe prompt
            self.prompt_buf.clear()
            self.dirty = True
            self.error_msg = ""
        elif str(val) in OPT_NUMBER_MAPPING:
            # change tabs with <OPT> + NUMERAL
            requested_idx = OPT_NUMBER_MAPPING[str(val)]
            if requested_idx < len(self.windows):
                self.current_window_name = list(self.windows.keys())[requested_idx]
                self.dirty = True
        elif val.code == curses.KEY_ENTER:
            # send it
            self.process_prompt()
        elif val:
            # just add entered character to buffer
            self.prompt_buf.append(str(val))
            self.dirty = True
        else:
            # the blessed inkey timeout will end up here - nothing to do
            pass

    def process_prompt(self) -> None:
        if not self.prompt_buf:
            return

        command, args = self.parse_prompt()
        coro = None

        if command == "exit":
            for client in self.clients:
                client.delay(client.quit(), self.loop)
            raise ExitInterrupt()
        elif command == "save":
            Transporter.beam_down(self)
        elif command == "quit":
            coro = self.client.quit()
        elif command == "list":
            coro = self.client.list()
        elif command == "part":
            if is_channel_name(self.current_window_name):
                coro = self.client.part_channel(self.current_window_name)
        elif command == "close":
            to_be_closed = self.current_window_name
            self.switch_window_relative(offset=-1)
            coro = self.client.delete(to_be_closed)
            del self.windows[to_be_closed]
        elif command in ("j", "join") and len(args) == 1:
            coro = self.client.join_channel(args[0])
        elif command == "nick" and len(args) == 1:
            coro = self.client.change_nick(args[0])
        elif command == "me":
            coro = self.client.send_ctcp_request(self.current_window_name, f"ACTION {' '.join(args)}")
        elif command == "msg" and len(args) > 1:
            recipient, text = args[0], " ".join(args[1:])
            coro = self.client.send_message(
                recipient=recipient, text=text, encrypt=recipient in self.client.keys
            )
        elif command == "ctcp" and len(args) > 1:
            recipient, text = args[0], " ".join(args[1:])
            coro = self.client.send_ctcp_request(recipient=recipient, text=text)
        elif command == "members":
            if is_channel_name(self.current_window_name):
                channel = self.client.channels[self.current_window_name]
                channel.buf.insert(IrcRawMessage(None, "MEMBERS", [" ".join(channel.users)]))
        elif command == "s":
            self.switch_client()
        elif command == "raw" and len(args) > 0:
            coro = self.client.send_cmd(args[0], args[1:])
        elif command:
            self.error_msg = f"Command unknown: {command} {args}"
        elif not command and args:
            # plain message to current window
            if self.is_server_window:
                self.error_msg = "Cannot send message to server window."
            else:
                coro = self.client.send_message(
                    recipient=self.current_window_name,
                    text=" ".join(args),
                    encrypt=self.current_window_name in self.client.keys,
                )

        # schedule response action as task inside the client eventloop.
        if coro:
            self.client.delay(coro, self.loop)
        self.prompt_buf.clear()
        self.dirty = True

    def parse_prompt(self) -> tuple[str, list[str]]:
        """Decompose input buffer into command and plain text"""
        prompt = "".join(self.prompt_buf).split()

        if not prompt:
            return "", []
        elif prompt[0].startswith("/"):
            return prompt[0].lstrip("/").lower(), prompt[1:]
        else:
            return "", prompt

    def sync_windows(self) -> None:
        """Check for new channels & chats in client, watch for topic updates"""
        # enforce server buf being initialized on UI start
        self.client.server_buf  # noqa: B018

        for channel in self.client.channels.values():
            if channel.name not in self.windows:
                self.windows[channel.name] = Window(channel.name, channel.buf)
                self.dirty = True
            window = self.windows[channel.name]
            if (new_header := f"[{channel.name}] {channel.topic}") != window.header:
                window.header = new_header
                self.dirty = True

        for chat_name, buf in self.client.chats.items():
            if chat_name not in self.windows:
                self.windows[chat_name] = Window(chat_name, buf)
                self.dirty = True
            window = self.windows[chat_name]
            if (new_header := f"[{chat_name}]") != window.header:
                window.header = new_header
                self.dirty = True

        # expose internal logging messages
        # if "log" not in self.windows:
        #     self.windows["log"] = Window("log", self.client.log_buf)

    def format_message(self, msg: IrcRawMessage, nick_offset: int = 0) -> str:
        date_str = self.t.webgray(f"[{msg.ts.strftime('%H:%M:%S')}]")
        colorizer = self.t.color_rgb(*name_to_rgb(msg.prefix_nick))
        colorized_nick = self.t.ljust(colorizer(f"<{msg.prefix_nick}>"), nick_offset + 2)
        divider = self.t.tomato("S") if msg.secure else self.t.webgray("|")

        if msg.command == "PRIVMSG":
            _target, text = msg.params
            return f"{date_str} {colorized_nick} {divider} {irc_to_ansi(text, self.t)}"
        elif msg.command == "NOTICE" or self.is_server_window:
            # 1. NOTICE can also be colorized, but still differentiate from PRIVMSG
            # 2. don't mute server window text
            if msg.params and msg.params[0] in (self.client.nick, "*"):
                text = " ".join(msg.params[1:])
            else:
                text = " ".join(msg.params)
            return f"{date_str} {colorized_nick} {divider} {self.t.webgray(msg.command):<4} {divider} {irc_to_ansi(text, self.t)}"
        else:
            # tone down non-text message in regular chats
            text = " ".join(msg.params)
            return f"{date_str} {colorized_nick} {divider} {self.t.webgray(f'{msg.command:<4} {divider} {text}')}"

    def render_interface_line(self, line: str) -> None:
        print(self.t.on_darkolivegreen(self.t.ljust(line, self.t.width)), end="")

    def render_dcc_line(self, dcc: DCC, name_offset: int) -> None:
        percentage = dcc.bytes_received / dcc.size
        title = self.t.ljust(f"{dcc.source}{self.t.gold2(':')} {dcc.filename} ", name_offset + 3)
        bar_width = self.t.width - self.t.length(title) - 2
        bar = (int(bar_width * percentage) * "#").ljust(bar_width)

        if dcc.verified:
            title = self.t.webgray(title)
            bar = self.t.webgray(bar)
        elif dcc.complete:
            bar = self.t.gold2(bar)
        else:
            bar = self.t.tomato(bar)

        self.render_interface_line(f"{title}{self.t.webgray('[')}{bar}{self.t.webgray(']')}")

    def render_dialog(self, text: str) -> None:
        text_width = len(text) + 4
        padded_text = self.t.center(self.error_msg, text_width)
        win_x = self.t.width // 2 - text_width // 2
        win_y = self.t.height // 2 - 1

        with self.t.location(win_x, win_y - 1):
            print(self.t.black_on_tomato(text_width * " "), end="")
        with self.t.location(win_x, win_y):
            print(self.t.black_on_tomato(padded_text), end="")
        with self.t.location(win_x, win_y + 1):
            print(self.t.black_on_tomato(text_width * " "), end="")

    def render(self) -> None:
        # skip re-render, if any visible changes had occurred, i.e. input, indicators, or current window content
        if (
            not self.dirty
            and not self.current_window.dirty_view
            and all(w.dirty_buf_before == w.dirty_buf for w in self.windows.values())
            and all(dcc.complete and dcc.verified for dcc in self.client.dcc)  # TODO
        ):
            return

        status_bar_height = min(len(self.client.dcc), 3)
        if status_bar_height:
            status_bar_height += 1

        self.dirty = False
        self._frame += 1

        # Topic line ------------------------------------------------------------------------------
        with self.t.location(0, 0):
            self.render_interface_line(irc_to_ansi(self.current_window.header, self.t))

        # chat window -----------------------------------------------------------------------------
        chat_window_height = self.t.height - 3 - status_bar_height
        buf_page = self.current_window.get_buf_page(chat_window_height)
        nick_offset = min(max([len(message.prefix_nick) for message in buf_page] or [0]), 50)
        if self.current_window.page == 0:
            self.current_window.reset_buf()

        for line_idx in range(chat_window_height):
            with self.t.location(0, chat_window_height - line_idx):
                print(self.t.clear_eol, end="")
                if line_idx < len(buf_page):
                    print(self.format_message(buf_page[line_idx], nick_offset), end="")
        # page indication
        if self.current_window.page != 0:
            with self.t.location(self.t.width - 3, 2):
                print(self.t.gold2(str(self.current_window.page)), end="")

        # tab line --------------------------------------------------------------------------------
        with self.t.location(0, self.t.height - 2 - status_bar_height):
            tab_line_items = []
            for idx, window in enumerate(self.windows.values()):
                window.dirty_buf_before = window.dirty_buf

                idx_indicator = f"[{idx + 1}]"
                if window.dirty_buf:
                    idx_indicator = self.t.black_on_gold2(idx_indicator)
                elif self.current_window_name != window.name:
                    idx_indicator = self.t.webgray(idx_indicator)
                tab = f"{idx_indicator} {window.name}"
                if self.current_window_name == window.name:
                    tab_line_items.append(self.t.black_on_tomato(tab))
                else:
                    tab_line_items.append(tab)
                tab_line = self.t.webgray(" - ").join(tab_line_items)

            client_selector = self.t.webgray(f"({self.client_idx + 1}/{len(self.clients)})")
            client_mode = "".join(self.client.mode)
            self.render_interface_line(
                f"{client_selector} {self.t.webgray('-')} {client_mode} {self.t.webgray('-')} {tab_line}"
            )

        # status ----------------------------------------------------------------------------------
        if status_bar_height:
            with self.t.location(0, self.t.height - 1 - status_bar_height):
                self.render_interface_line(self.t.webgray(self.t.width * "\u2014"))
            recent_dccs = self.client.dcc[-3:]
            dcc_name_offset = max(len(dcc.filename) + len(dcc.source) for dcc in recent_dccs)
            for idx, dcc in enumerate(reversed(recent_dccs)):
                with self.t.location(0, self.t.height - 2 - idx):
                    self.render_dcc_line(dcc, dcc_name_offset)

        # prompt ----------------------------------------------------------------------------------
        with self.t.location(0, self.t.height - 1):
            prompt = "".join(self.prompt_buf)
            command, args = self.parse_prompt()

            secure_adhoc_target = command == "msg" and len(args) > 1 and args[0] in self.client.keys
            secure_target = self.current_window_name in self.client.keys
            secured = self.t.tomato(" secured >>") if secure_adhoc_target or secure_target else ""

            print(self.t.clear_eol + f"[{self.client.nick}]{secured} {prompt}", end="")

        if self.error_msg:
            self.render_dialog(self.error_msg)

        # DEBUG: frame-counter
        # with self.t.location(self.t.width - 8, self.t.height - 1):
        #     print(f"f:{self._frame}", end="")

    def run(self) -> None:
        with self.t.fullscreen(), self.t.cbreak(), self.t.hidden_cursor():
            print(self.t.home + self.t.clear, end="")
            val = Keystroke()
            while True:
                try:
                    self.sync_windows()
                    self.process_input(val)
                    self.render()
                except ExitInterrupt:
                    return
                except KeyboardInterrupt:
                    self.client.log("Keyboard interrupt")
                    return
                except Exception as e:
                    self.client.log(f"something went terribly wrong: {e}")
                    self.client.log(traceback.format_exc())
                # wait for next input
                val = self.t.inkey(timeout=0.33)
