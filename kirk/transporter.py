import json
import os
import typing
from dataclasses import asdict
from datetime import datetime
from typing import Any

from kirk.client import Buffer, IrcRawMessage

if typing.TYPE_CHECKING:
    from kirk.kirk import Kirk


class Transporter:
    @classmethod
    def beam_down(cls, kirk: "Kirk") -> None:
        """save state of kirk session"""
        data = {}
        for client in kirk.clients:
            data[client.host] = {
                "chats": {name: cls._serialize(buf) for name, buf in client.chats.items()},
                "channels": {chan.name: cls._serialize(chan.buf) for chan in client.channels.values()},
            }
        with open(os.path.expanduser("~/.kirk_state.json"), "w") as fh:
            json.dump(data, fh, indent=1, default=str)

    @classmethod
    def beam_up(cls, kirk: "Kirk") -> None:
        """load state of previous kirk session"""
        try:
            with open(os.path.expanduser("~/.kirk_state.json")) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return

        clients = {c.host: c for c in kirk.clients}
        for client_host, client_data in data.items():
            if not (client := clients.get(client_host)):
                continue
            for name, msgs in client_data["chats"].items():
                cls._deserialize(client.chats[name], msgs)
            for name, msgs in client_data["channels"].items():
                cls._deserialize(client.channels[name].buf, msgs)

    @classmethod
    def _deserialize(cls, buf: Buffer[IrcRawMessage], msgs: list[dict[str, Any]]) -> None:
        for msg in reversed(msgs):
            msg["ts"] = datetime.fromisoformat(msg["ts"])
            buf.insert(IrcRawMessage(**msg))
        buf.insert(IrcRawMessage(None, "SAVE", [80 * "-"]))

    @classmethod
    def _serialize(cls, buf: Buffer[Any]) -> list[dict[str, Any]]:
        return [asdict(m) for m in buf]
