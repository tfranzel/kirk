import tomlkit

from kirk import config


def test_persist_key_writes_new_key_for_listed_host(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text(
        '[kirk]\n\n[[kirk.client]]\nhost = "irc.example.com"\nnick = "alice"\n'
        '\n[[kirk.client]]\nhost = "irc.other.com"\nnick = "someone"\n'
    )

    config.persist_key(str(config_path), "irc.example.com", "bob", "freshkey")

    doc = tomlkit.parse(config_path.read_text())
    entries = {c["host"]: c for c in doc["kirk"]["client"]}
    assert entries["irc.example.com"]["keys"]["bob"] == "freshkey"
    assert "keys" not in entries["irc.other.com"]


def test_persist_key_updates_existing_key(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text(
        '[kirk]\n\n[[kirk.client]]\nhost = "irc.example.com"\nnick = "alice"\nkeys = { bob = "oldkey" }\n'
    )

    config.persist_key(str(config_path), "irc.example.com", "bob", "newkey")

    doc = tomlkit.parse(config_path.read_text())
    assert doc["kirk"]["client"][0]["keys"]["bob"] == "newkey"


def test_persist_key_noop_when_host_absent_from_config(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text('[kirk]\n\n[[kirk.client]]\nhost = "irc.other.com"\nnick = "bob"\n')

    config.persist_key(str(config_path), "irc.example.com", "bob", "somekey")

    # untouched: our host was never listed in this config
    assert "somekey" not in config_path.read_text()
