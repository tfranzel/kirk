import importlib.util
import typing
from pathlib import Path
from typing import cast

import tomlkit

if typing.TYPE_CHECKING:
    from kirk.client import IrcClient

SPECIAL_USERS = ["NickServ", "HostServ", "ChanServ", "SaslServ"]


def persist_key(config_path: str, host: str, recipient: str, key: str) -> None:
    """
    Write `key` for `recipient` into the config file's entry for `host`, so it
    survives a restart. Does nothing if `host` isn't listed there. Propagates
    OSError/tomlkit errors to the caller.
    """
    with open(config_path, encoding="utf-8") as fh:
        doc = tomlkit.parse(fh.read())

    entry = next((c for c in doc["kirk"]["client"] if c.get("host") == host), None)
    if entry is None:
        return

    keys = entry.get("keys")
    if keys is None:
        entry["keys"] = keys = tomlkit.inline_table()
    keys[recipient] = key

    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(tomlkit.dumps(doc))


def is_ctcp(text: str) -> bool:
    return text.startswith("\x01") and text.endswith("\x01")


def resolve_dcc_path(dcc_dir: str, filename: str) -> Path:
    """
    Resolve *filename* -- attacker-controlled, taken verbatim from an incoming DCC SEND
    offer -- to a path strictly inside *dcc_dir*, refusing it outright if it isn't already
    a bare filename.

    A legitimate DCC filename never contains a path separator, so anything that does --
    ``../../etc/passwd``, an absolute path like ``/etc/passwd`` (pathlib's ``/`` operator
    would otherwise silently discard *dcc_dir* entirely for an absolute right-hand side),
    or even an innocuous-looking ``subdir/file`` -- is rejected rather than silently
    stripped down to its basename: quietly rewriting it could mask an attack and risks a
    basename collision with an unrelated, already-downloaded file. The final resolved path
    is additionally verified to still be a descendant of *dcc_dir* as defense in depth.
    """
    base = Path(dcc_dir).expanduser().resolve()
    name = Path(filename).name
    if not filename or filename != name or name in (".", ".."):
        raise ValueError(f"invalid DCC filename: {filename!r}")

    candidate = (base / name).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"DCC filename escapes {base}: {filename!r}")
    return candidate


def load_client_class(spec: str, config_path: str) -> type["IrcClient"]:
    """Load a custom IrcClient subclass from a 'path/to/file.py:ClassName' spec."""
    file_part, _, class_name = spec.rpartition(":")
    if not file_part or not class_name:
        raise ValueError(f"client_class must be 'path/to/file.py:ClassName', got {spec!r}")

    file_path = Path(file_part).expanduser()
    if not file_path.is_absolute():
        file_path = Path(config_path).expanduser().parent / file_path

    module_spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot load module from {file_path}")

    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return cast("type[IrcClient]", getattr(module, class_name))
