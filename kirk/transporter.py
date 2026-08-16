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
    """Handles saving and loading of Kirk session state to/from disk."""

    @classmethod
    def beam_down(cls, kirk: "Kirk") -> None:
        """Save current Kirk session state to disk."""
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
        """Restore Kirk session state from disk."""
        try:
            with open(os.path.expanduser("~/.kirk_state.json")) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as e:
            kirk.error_msg = f"Could not read ~/.kirk_state.json, skipping restore: {e}"
            return

        clients = {c.host: c for c in kirk.clients}
        try:
            for client_host, client_data in data.items():
                if not (client := clients.get(client_host)):
                    continue
                for name, msgs in client_data["chats"].items():
                    cls._deserialize(client.chats[name], msgs)
                for name, msgs in client_data["channels"].items():
                    cls._deserialize(client.channels[name].buf, msgs)
        except (KeyError, TypeError, ValueError) as e:
            kirk.error_msg = f"~/.kirk_state.json is incompatible with this version of Kirk, skipping restore: {e}"

    @classmethod
    def _deserialize(cls, buf: Buffer[IrcRawMessage], msgs: list[dict[str, Any]]) -> None:
        for msg in msgs:
            msg["ts"] = datetime.fromisoformat(msg["ts"])
            buf.insert(IrcRawMessage(**msg))
        for _ in range(3):
            buf.insert(IrcRawMessage(None, "SAVE", [120 * "-"]))

    @classmethod
    def _serialize(cls, buf: Buffer[Any]) -> list[dict[str, Any]]:
        return [asdict(m) for m in reversed(buf)]
