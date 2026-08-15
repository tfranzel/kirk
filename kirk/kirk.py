import curses
import itertools
import traceback
from asyncio import AbstractEventLoop
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from blessed import Terminal
from blessed.keyboard import Keystroke

from kirk.client import (
    CHANNEL_PERM_MAPPING,
    DCC,
    Buffer,
    ChannelUserPerm,
    IrcClient,
    IrcRawMessage,
)
from kirk.color import highlight_mentions, irc_to_ansi, name_to_rgb
from kirk.help import HELP_TEXT
from kirk.transporter import Transporter

OPT_NUMBER_MAPPING = dict(zip("¡™£¢∞§¶•ªº", range(10), strict=False))


class ExitInterrupt(Exception):
    """Raised when user requests exit from UI."""

    pass


@dataclass
class Window:
    """Represents a scrollable window tab with buffer state."""

    name: str
    buf: Buffer[IrcRawMessage]
    buf_idx_viewed: int = 0
    buf_idx_frozen: int | None = None
    page: int = 0
    header: str = ""
    dirty_buf_before: bool = False

    def page_up(self) -> None:
        """Scroll up one page, freezing buffer view."""
        if self.page == 0:
            # freeze buffer view on scroll start
            self.buf_idx_frozen = self.buf_idx_viewed
        self.page += 1

    def page_down(self) -> None:
        """Scroll down one page, unfreezing at bottom."""
        self.page = max(0, self.page - 1)
        if self.page == 0:
            # release buffer fix on scroll end
            self.buf_idx_frozen = None

    def page_reset(self) -> None:
        """Reset to bottom of buffer."""
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
        """Whether the live view has changed due to buffer changes."""
        return self.page == 0 and self.dirty_buf

    @property
    def dirty_buf(self) -> bool:
        """Whether messages were added to buf since it was last displayed."""
        return self.buf_idx_viewed != self.buf.idx

    def reset_buf(self) -> None:
        """Mark current state of buffer as viewed."""
        self.buf_idx_viewed = self.buf.idx


class Kirk:
    """Terminal-based UI for IRC clients with tabbed windows and scrolling."""

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
        self.sync_client()

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
        elif val.name == "RESIZE_EVENT":
            self.dirty = True
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
        elif val.is_sequence:
            # Do nothing for multibyte sequences (e.g. uncaught modifiers, arrow keys, etc.).
            # This is safe for multibyte Unicode (é, 日) and single-codepoint emoji
            pass
        elif val:
            self.prompt_buf.append(str(val))
            self.dirty = True
        else:
            # the blessed inkey timeout will end up here - nothing to do
            pass

    def process_prompt(self) -> None:
        if not self.prompt_buf:
            return

        command, args = self.parse_prompt()
        coro: Coroutine[Any, Any, None] | None = None

        if command == "exit":
            for client in self.clients:
                client.delay(client.quit(), self.loop)
            raise ExitInterrupt()
        elif command == "save":
            Transporter.beam_down(self)
        elif command in ("help", "h"):
            for line in HELP_TEXT.split("\n"):
                self.current_window.buf.insert(IrcRawMessage(prefix=None, command="HELP", params=[line]))
        elif command in ("q", "quit"):
            coro = self.client.quit()
        elif command in ("l", "list"):
            coro = self.client.list()
        elif command in ("w", "whois") and len(args) == 1:
            coro = self.client.whois(args[0])
        elif command == "part":
            if self.client.is_channel_name(self.current_window_name):
                coro = self.client.part_channel(self.current_window_name)
        elif command == "close":
            to_be_closed = self.current_window
            self.switch_window_relative(offset=-1)
            coro = self.client.delete(to_be_closed.name)
            del self.windows[to_be_closed.name]
        elif command in ("j", "join") and len(args) == 1:
            coro = self.client.join_channel(args[0])
        elif command == "nick" and len(args) == 1:
            coro = self.client.change_nick(args[0])
        elif command == "me":
            coro = self.client.send_ctcp_request(self.current_window_name, f"ACTION {' '.join(args)}")
        elif command in ("m", "msg") and len(args) > 1:
            recipient, text = args[0], " ".join(args[1:])
            coro = self.client.send_message(
                recipient=recipient, text=text, encrypt=recipient in self.client.keys
            )
        elif command == "ctcp" and len(args) > 1:
            recipient, text = args[0], " ".join(args[1:])
            coro = self.client.send_ctcp_request(recipient=recipient, text=text)
        elif command == "members":
            if self.client.is_channel_name(self.current_window_name):
                channel = self.client.channels[self.current_window_name]
                users = map(
                    lambda pu: f"{CHANNEL_PERM_MAPPING[pu[0]]}{pu[1]}",
                    sorted((max(p or {ChannelUserPerm.BASE}), u) for u, p in channel.users.items()),
                )
                for batch in itertools.batched(users, self.t.width // 15):
                    channel.buf.insert(IrcRawMessage(None, "MEMBERS", [" ".join(batch)]))
        elif command == "grep":
            target_buf = self.client.get_buf(self.current_window_name)
            filtered_buf = Buffer[IrcRawMessage](target_buf.size)
            filter_term = " ".join(args).lower()
            for m in target_buf:
                if filter_term in "".join(m.params).lower():
                    filtered_buf.insert(m)
            self.windows[f"grep {filter_term}"] = Window(f"grep {filter_term}", filtered_buf)
        elif command == "switch":
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

    def sync_client(self) -> None:
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

        if self.client.attention_requested:
            print("\x07", end="")

    def format_message(
        self, msg: IrcRawMessage, nick_offset: int = 0, cmd_offset: int = 0
    ) -> tuple[tuple[str, str], ...]:
        date_str = self.t.webgray(f"[{msg.ts.strftime('%H:%M:%S')}]")
        colorizer = self.t.color_rgb(*name_to_rgb(msg.prefix_nick))
        colorized_nick = self.t.ljust(colorizer(f"<{msg.prefix_nick}>"), nick_offset + 2)
        divider = self.t.tomato("S") if msg.secure else self.t.webgray("|")

        if msg.command == "PRIVMSG":
            _target, text = msg.params
            cmd = ""
            text = highlight_mentions(text, self.client.nick, self.t)
            body = irc_to_ansi(text, self.t)
        elif msg.command == "NOTICE" or self.is_server_window:
            # 1. NOTICE can also be colorized, but still differentiate from PRIVMSG
            # 2. don't mute server window text
            if msg.params and msg.params[0] in (self.client.nick, "*"):
                text = " ".join(msg.params[1:])
            else:
                text = " ".join(msg.params)

            cmd = self.t.webgray(msg.command)
            text = highlight_mentions(text, self.client.nick, self.t)
            body = irc_to_ansi(text, self.t)
        elif msg.command == "TAGMSG":
            cmd = self.t.webgray(msg.command)
            body = self.t.webgray(str(msg.tags))
        else:
            # tone down non-text message in regular chats
            cmd = self.t.webgray(msg.command)
            body = self.t.webgray(" ".join(msg.params))

        return self._split_message(
            head=f"{date_str} {colorized_nick} {divider} {self.t.ljust(cmd, cmd_offset)} {divider} ",
            body=body,
            divider=divider,
        )

    def _split_message(self, head: str, body: str, divider: str) -> tuple[tuple[str, str], ...]:
        """Split messages that are too long for terminal into chunks"""
        head_len = self.t.length(head)
        body_len = self.t.length(body)

        if body_len + head_len > self.t.width:
            body_width = self.t.width - head_len
            body_split = tuple(body[i : i + body_width] for i in range(0, len(body), body_width))
            head_split = (head,) + tuple(
                self.t.rjust(f"{divider} ", head_len) for _ in range(len(body_split) - 1)
            )
            return tuple(zip(head_split, body_split, strict=True))
        else:
            return ((head, body),)

    def render_interface_line(self, line: str) -> None:
        print(self.t.on_darkolivegreen(self.t.ljust(line, self.t.width)), end="")

    def render_dcc_line(self, dcc: DCC, name_offset: int, total_width: int) -> None:
        percentage = dcc.bytes_received / dcc.size
        title = self.t.ljust(f"{dcc.source}{self.t.gold2(':')} {dcc.filename} ", name_offset + 3)
        bar_width = total_width - self.t.length(title) - 2
        bar = (int(bar_width * percentage) * "#").ljust(bar_width)

        if dcc.verified:
            title = self.t.webgray(title)
            bar = self.t.webgray(bar)
        elif dcc.complete:
            bar = self.t.gold2(bar)
        else:
            bar = self.t.tomato(bar)

        print(f"{title}{self.t.webgray('[')}{bar}{self.t.webgray(']')}", end="")

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
        # skip re-render, if no visible changes had occurred, i.e. input, indicators, or current window content
        if (
            not self.dirty
            and not self.current_window.dirty_view
            and all(w.dirty_buf_before == w.dirty_buf for w in self.windows.values())
            and all(dcc.complete for dcc in self.client.dcc)  # TODO
        ):
            return

        self.dirty = False
        self._frame += 1

        self.render_topic_line()
        self.render_chat_window()
        self.render_tabs()
        self.render_dcc()
        self.render_prompt()

        if self.error_msg:
            self.render_dialog(self.error_msg)

        # DEBUG: frame-counter
        # with self.t.location(self.t.width - 8, self.t.height - 1):
        #     print(f"f:{self._frame}", end="")

    def render_topic_line(self) -> None:
        with self.t.location(0, 0):
            self.render_interface_line(irc_to_ansi(self.current_window.header, self.t))

    def render_box(self, inner_height: int, inner_width: int) -> tuple[int, int, int, int]:
        """Draw a box with '-' edges and '+' corners, anchored to the top-right of the window."""
        width = inner_width + 2
        height = inner_height + 2  # inner rows + top/bottom border
        right = self.t.width - 1
        top = 1
        left = right - width + 1
        bottom = top + height - 1

        with self.t.location(left, top):
            print(self.t.darkolivegreen("+" + "-" * (width - 2) + "+"), end="")
        for y in range(top + 1, bottom):
            with self.t.location(left, y):
                print(self.t.darkolivegreen("|"), end="")
            with self.t.location(right, y):
                print(self.t.darkolivegreen("|"), end="")
        with self.t.location(left, bottom):
            print(self.t.darkolivegreen("+" + "-" * (width - 2) + "+"), end="")

        return top, left, bottom, right

    def render_dcc(self) -> None:
        if not self.client.dcc:
            return

        recent_dccs = self.client.dcc[-5:]
        name_column_width = max(len(dcc.filename) + len(dcc.source) for dcc in recent_dccs)

        top, left, bottom, right = self.render_box(
            inner_height=len(recent_dccs),
            inner_width=max([name_column_width + 5 + 10, self.t.width * 2 // 5]),
        )

        for idx, dcc in enumerate(reversed(recent_dccs)):
            with self.t.location(left + 1, top + 1 + idx):
                self.render_dcc_line(dcc, name_column_width, right - left - 1)

    def render_chat_window(self) -> None:
        chat_window_height = self.t.height - 3
        buf_page = self.current_window.get_buf_page(chat_window_height)

        nick_offset = max([len(message.prefix_nick) for message in buf_page] or [0])
        cmd_offset = max(
            [len(message.command) for message in buf_page if message.command != "PRIVMSG"] or [0]
        )

        if self.current_window.page == 0:
            self.current_window.reset_buf()

        line_idx = chat_window_height
        buf_idx = 0
        while line_idx >= 1:
            if buf_idx >= len(buf_page):
                # no more messages. clear line
                with self.t.location(0, line_idx):
                    print(self.t.clear_eol, end="")
                line_idx -= 1
            else:
                lines = self.format_message(buf_page[buf_idx], nick_offset, cmd_offset)
                buf_idx += 1
                for head, body in reversed(lines):
                    with self.t.location(0, line_idx):
                        print(self.t.clear_eol + f"{head}{body}", end="")
                        line_idx -= 1

        # page indication
        if self.current_window.page != 0:
            with self.t.location(self.t.width - 5, self.t.height - 4):
                print(self.t.on_darkolivegreen(self.t.rjust(str(self.current_window.page), 3)), end="")

    def render_tabs(self) -> None:
        with self.t.location(0, self.t.height - 2):
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

    def render_prompt(self) -> None:
        with self.t.location(0, self.t.height - 1):
            prompt = "".join(self.prompt_buf)
            command, args = self.parse_prompt()

            secure_adhoc_target = command == "msg" and len(args) > 1 and args[0] in self.client.keys
            secure_target = self.current_window_name in self.client.keys
            secured = self.t.tomato(" secured >>") if secure_adhoc_target or secure_target else ""

            print(self.t.clear_eol + f"[{self.client.nick}]{secured} {prompt}", end="")

    def run(self) -> None:
        with self.t.fullscreen(), self.t.cbreak(), self.t.hidden_cursor(), self.t.notify_on_resize():
            print(self.t.home + self.t.clear, end="")
            val = Keystroke()
            while True:
                try:
                    self.sync_client()
                    self.process_input(val)
                    self.render()
                    # wait for next input
                    val = self.t.inkey(timeout=0.33)
                except ExitInterrupt:
                    return
                except Exception as e:
                    self.client.log(f"something went terribly wrong: {e}")
                    self.client.log(traceback.format_exc())
