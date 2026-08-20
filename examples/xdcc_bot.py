import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from kirk.client import IrcClient, IrcRawMessage

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


class XdccBot(IrcClient):
    """Implements the XDCC protocol."""

    _offering_map: dict[int, tuple[int, Path]] = {}

    def __init__(
        self,
        host: str,
        nick: str,
        dcc_announce_channel: str,
        dcc_serve_path: Path,
        dcc_host_ip: str,
        **kwargs: Any,
    ) -> None:
        self.dcc_announce_channel = dcc_announce_channel
        self.dcc_serve_path = dcc_serve_path
        kwargs.setdefault("auto_join", [dcc_announce_channel])
        kwargs.setdefault("log_mode", "console")
        super().__init__(host=host, nick=nick, dcc_host_ip=dcc_host_ip, **kwargs)

    def build_offering_map(self) -> None:
        """Rebuild the announced file map from serve_dir, replacing any previous scan."""
        self._offering_map = dict(
            enumerate(sorted((p.stat().st_size, p) for p in self.dcc_serve_path.iterdir() if p.is_file()))
        )

    async def announce_periodically(self, interval_minutes: int) -> None:
        """Rescan serve_dir and re-announce available files on a fixed interval."""
        await asyncio.sleep(10)

        while True:
            self.build_offering_map()

            await self.send_message(
                self.dcc_announce_channel,
                f'\x02**\x02 To request a file, type "/MSG {self.nick} XDCC (S)SEND x"\x02**\x02',
            )

            for idx, (size, path) in self._offering_map.items():
                await self.send_message(
                    self.dcc_announce_channel,
                    f"\x02#{idx:<4}\x02 0x [{self.format_size(size)}] {path.name}",
                )
                await asyncio.sleep(1)

            await asyncio.sleep(interval_minutes * 60)

    async def process_user_message(self, message: IrcRawMessage) -> None:
        await super().process_user_message(message)

        _, text = message.params
        requester = message.prefix_nick

        match = re.match(r"^xdcc (?P<mode>s?send) #(?P<idx>\d+)$", text.strip(), re.IGNORECASE)
        offering = self._offering_map.get(int(match["idx"])) if match else None

        if offering and match:
            size, file = offering
            self.log(f"Resolved {match['idx']} to {file.name} ({self.format_size(size)}), sending ...")
            await self.dcc_send(requester, file, ssl=match["mode"].lower() == "ssend")
        else:
            await self.send_message(requester, "Invalid request")

    @classmethod
    def format_size(cls, size: int) -> str:
        """Format a byte count as megabytes, or gigabytes above 1024 MB."""
        if size < 1024:
            return f"{size / 1024:.0f}K"
        if size < 1024**3:
            return f"{size / 1024**2:.0f}M"
        return f"{size / 1024**3:.1f}G"


async def main() -> None:
    bot = XdccBot(
        host="irc.libera.chat",
        nick="SOMENAME",
        auto_join=["#SOMECHANNEL"],
        dcc_host_ip="127.0.0.1",
        log_mode="console",
        dcc_announce_channel="#SOMECHANNEL",
        dcc_serve_path=Path("/SOME/PATH/"),
    )
    await asyncio.gather(bot.run(), bot.announce_periodically(interval_minutes=10))


if __name__ == "__main__":
    asyncio.run(main(), debug=False)
