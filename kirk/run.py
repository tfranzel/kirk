import asyncio
import importlib
import os
import tomllib

from kirk.client import IrcClient
from kirk.kirk import Kirk
from kirk.transporter import Transporter


async def main() -> None:
    with open(os.path.expanduser("~/.kirk.toml"), "rb") as fh:
        config = tomllib.load(fh)

    if "client_class" in config["kirk"]:
        *path, class_name = config["kirk"]["client_class"].split(".")
        client_class: type[IrcClient] = getattr(importlib.import_module(".".join(path), "."), class_name)
    else:
        client_class = IrcClient
    persistence = config["kirk"].get("persistence", False)

    loop = asyncio.get_running_loop()
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
    kirk = Kirk(clients, loop)
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
    asyncio.run(main(), debug=False)
