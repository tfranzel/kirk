"""Read/write access to the kirk.toml config file backing a running client."""

import tomlkit


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
