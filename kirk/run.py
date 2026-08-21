import argparse
import asyncio
import os
import signal
import tomllib

from kirk.client import IrcClient
from kirk.kirk import Kirk
from kirk.transporter import Transporter
from kirk.utils import load_client_class


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kirk",
        description="Kirk - a minimalistic IRC client and curses UI.",
    )
    parser.add_argument(
        "--host",
        help="IRC server host to connect to, e.g. irc.libera.chat.",
    )
    parser.add_argument("--nick", help="Nickname to use on the server.")
    parser.add_argument(
        "--no-ssl",
        dest="ssl",
        action="store_false",
        default=True,
        help="Connect without SSL/TLS.",
    )
    parser.add_argument(
        "--join",
        nargs="+",
        metavar="CHANNEL",
        help="Channel(s) to auto-join, e.g. --join #chan1 #chan2.",
    )
    parser.add_argument(
        "--auth",
        choices=["nickserv", "sasl_plain"],
        help="Authentication method to use after connecting. (default: sasl_plain)",
    )
    parser.add_argument(
        "--password",
        help="Password to use with given auth method.",
    )
    parser.add_argument(
        "--key",
        action="append",
        metavar="TARGET=FERNETKEY",
        help=(
            "E2E encryption key for a channel or nick, e.g. --key '#chan1=KEY'. Repeat for multiple targets. "
            "Needs to be a 32 byte base64 encoded string, e.g. base64.urlsafe_b64encode(os.urandom(32)). "
            "Other party needs the exakt same key."
        ),
    )
    parser.add_argument(
        "--dcc-dir",
        help="Directory to save incoming DCC file transfers to.",
    )
    parser.add_argument(
        "--dcc-host-ip",
        help="Externally reachable IP to advertise when offering files via '/dcc send'.",
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="Log session activity to file instead of discarding it.",
    )
    parser.add_argument(
        "--persistence",
        action="store_true",
        help="Save buffer history on exit and load it again on start.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to the TOML config file (default: ~/.kirk.toml). "
        "Mutually exclusive with all other options!",
    )
    args = parser.parse_args()

    if args.config and any(
        [
            args.host,
            args.nick,
            args.join,
            args.key,
            args.log,
            args.dcc_dir,
            args.dcc_host_ip,
            args.auth,
            args.password,
            args.persistence,
            not args.ssl,
        ]
    ):
        parser.error("argument --config: not allowed with any other argument")

    keys = {}
    for entry in args.key or []:
        target, sep, key = entry.partition("=")
        if not sep:
            parser.error(f"argument --key: expected TARGET=FERNETKEY, got {entry!r}")
        keys[target] = key
    args.key = keys

    return args


async def _main(args: argparse.Namespace) -> None:
    """Initialize and run Kirk IRC client from CLI arguments or a configuration file."""
    if args.host and args.nick:
        persistence = args.persistence
        clients = [
            IrcClient(
                host=args.host,
                nick=args.nick,
                ssl=args.ssl,
                auth=args.auth or "sasl_plain",
                password=args.password,
                auto_join=args.join or [],
                keys=args.key,
                dcc_dir=os.path.expanduser(args.dcc_dir) if args.dcc_dir else None,
                dcc_host_ip=args.dcc_host_ip,
                log_mode="file" if args.log else "none",
            )
        ]
    else:
        config_path = os.path.expanduser(args.config or "~/.kirk.toml")
        try:
            with open(config_path, "rb") as fh:
                config = tomllib.load(fh)
        except FileNotFoundError:
            print(
                f"You either need to provide at least --host and --nick arguments OR a config "
                f"file must exist. \nWe search for it at {os.path.expanduser('~/.kirk.toml')}, "
                f"but you can also give another location with --config PATH.\n\nSee here for examples: "
                f"https://github.com/tfranzel/kirk#non-exhaustive-example-for-config-file-kirktoml"
                f"\n\nRun `kirk --help` for more information.\n"
            )
            exit(1)

        if "client_class" in config["kirk"]:
            client_class: type[IrcClient] = load_client_class(config["kirk"]["client_class"], config_path)
        else:
            client_class = IrcClient

        persistence = config["kirk"].get("persistence", False)

        clients = [
            client_class(
                host=c["host"],
                nick=c["nick"],
                auth=c.get("auth", "sasl_plain"),
                password=c.get("password", None),
                auto_join=c.get("auto_join", None),
                keys=c.get("keys", {}),
                dcc_dir=os.path.expanduser(c.get("dcc_dir", "~/Downloads/")),
                dcc_host_ip=c.get("dcc_host_ip", None),
                ssl=c.get("ssl", True),
                log_mode=c.get("log_mode", "none"),
                config_path=config_path,
            )
            for c in config["kirk"]["client"]
        ]

    loop = asyncio.get_running_loop()
    kirk = Kirk(clients, loop)

    # blessed's notify_on_resize() relies on the terminal supporting in-band
    # resize notifications (DEC mode 2048); some do not implement that, so
    # fall back to SIGWINCH to still pick up resizes when running inside it.
    if not kirk.t.does_inband_resize():
        signal.signal(signal.SIGWINCH, lambda *_: setattr(kirk, "dirty", True))

    # Ctrl-C should never kill the app outright (too easy to hit by accident);
    # just nudge the user towards the real exit command instead of relying on
    # the default SIGINT -> KeyboardInterrupt behavior.
    def _deny_sigint_exit() -> None:
        kirk.error_msg = "Kirk can be exited by typing '/exit'"
        kirk.dirty = True

    loop.add_signal_handler(signal.SIGINT, _deny_sigint_exit)

    if persistence:
        print("Beaming up crew ...")
        Transporter.beam_up(kirk)
    print("1/2 impulse forward ...")
    _ = loop.run_in_executor(None, kirk.run)
    try:
        await asyncio.gather(*(c.run() for c in clients))
    finally:
        if persistence:
            print("Beaming down crew ...")
            Transporter.beam_down(kirk)
    print("Mission complete!")


def main() -> None:
    asyncio.run(_main(parse_args()), debug=False)


if __name__ == "__main__":
    main()
