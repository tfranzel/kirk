import argparse
import asyncio
import importlib
import os
import signal
import tomllib

from kirk.client import IrcClient
from kirk.kirk import Kirk
from kirk.transporter import Transporter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kirk",
        description="Kirk - a minimalistic IRC client and curses UI.",
    )
    parser.add_argument(
        "--host",
        help="IRC server host to connect to, e.g. irc.libera.chat. "
        "If given together with --nick, ~/.kirk.toml is ignored.",
    )
    parser.add_argument("--nick", help="Nickname to use on the server.")
    parser.add_argument(
        "--no-ssl", dest="ssl", action="store_false", default=True, help="Connect without SSL/TLS."
    )
    parser.add_argument(
        "--log", action="store_true", help="Log session activity to file instead of discarding it."
    )
    parser.add_argument(
        "--config",
        default="~/.kirk.toml",
        help="Path to the TOML config file (default: ~/.kirk.toml). Ignored if --host and --nick are given.",
    )
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    """Initialize and run Kirk IRC client from CLI arguments or a configuration file."""
    persistence = False

    if args.host and args.nick:
        clients = [
            IrcClient(
                host=args.host,
                nick=args.nick,
                ssl=args.ssl,
                log_mode="file" if args.log else "none",
            )
        ]
    else:
        with open(os.path.expanduser(args.config), "rb") as fh:
            config = tomllib.load(fh)

        if "client_class" in config["kirk"]:
            *path, class_name = config["kirk"]["client_class"].split(".")
            client_class: type[IrcClient] = getattr(
                importlib.import_module(".".join(path), "."), class_name
            )
        else:
            client_class = IrcClient
        persistence = config["kirk"].get("persistence", False)

        clients = [
            client_class(
                host=c["host"],
                nick=c["nick"],
                auth=c.get("auth", None),
                auto_join=c.get("auto_join", None),
                keys=c.get("keys", {}),
                dcc_dir=os.path.expanduser(c.get("dcc_dir", "~/Downloads/")),
                ssl=c.get("ssl", True),
                log_mode=c.get("log_mode", "none"),
            )
            for c in config["kirk"]["client"]
        ]

    loop = asyncio.get_running_loop()
    kirk = Kirk(clients, loop)

    # blessed's notify_on_resize() relies on the terminal supporting in-band
    # resize notifications (DEC mode 2048); tmux does not implement that, so
    # fall back to SIGWINCH to still pick up resizes when running inside it.
    if not kirk.t.does_inband_resize():
        signal.signal(signal.SIGWINCH, lambda *_: setattr(kirk, "dirty", True))

    if persistence:
        print("Beaming up crew ...")
        Transporter.beam_up(kirk)
    print("1/2 impulse forward ...")
    _ = loop.run_in_executor(None, kirk.run)
    print("Priming warp drive ...")
    try:
        await asyncio.gather(*(c.run() for c in clients))
    finally:
        if persistence:
            print("Beaming down crew ...")
            Transporter.beam_down(kirk)
    print("Mission complete!")


if __name__ == "__main__":
    asyncio.run(main(parse_args()), debug=False)
