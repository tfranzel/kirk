import pytest
import tomlkit

from kirk.client import IrcClient
from kirk.utils import load_client_class, persist_key


def test_persist_key_writes_new_key_for_listed_host(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text(
        '[kirk]\n\n[[kirk.client]]\nhost = "irc.example.com"\nnick = "alice"\n'
        '\n[[kirk.client]]\nhost = "irc.other.com"\nnick = "someone"\n'
    )

    persist_key(str(config_path), "irc.example.com", "bob", "freshkey")

    doc = tomlkit.parse(config_path.read_text())
    entries = {c["host"]: c for c in doc["kirk"]["client"]}
    assert entries["irc.example.com"]["keys"]["bob"] == "freshkey"
    assert "keys" not in entries["irc.other.com"]


def test_persist_key_updates_existing_key(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text(
        '[kirk]\n\n[[kirk.client]]\nhost = "irc.example.com"\nnick = "alice"\nkeys = { bob = "oldkey" }\n'
    )

    persist_key(str(config_path), "irc.example.com", "bob", "newkey")

    doc = tomlkit.parse(config_path.read_text())
    assert doc["kirk"]["client"][0]["keys"]["bob"] == "newkey"


def test_persist_key_noop_when_host_absent_from_config(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text('[kirk]\n\n[[kirk.client]]\nhost = "irc.other.com"\nnick = "bob"\n')

    persist_key(str(config_path), "irc.example.com", "bob", "somekey")

    # untouched: our host was never listed in this config
    assert "somekey" not in config_path.read_text()


def test_load_client_class_relative_path_resolves_against_config_dir(tmp_path):
    config_path = tmp_path / "kirk.toml"
    (tmp_path / "custom_client.py").write_text(
        "from kirk.client import IrcClient\n\nclass MyClient(IrcClient):\n    pass\n"
    )

    client_class = load_client_class("custom_client.py:MyClient", str(config_path))

    assert issubclass(client_class, IrcClient)
    assert client_class.__name__ == "MyClient"


def test_load_client_class_absolute_path(tmp_path):
    custom_client = tmp_path / "nested" / "custom_client.py"
    custom_client.parent.mkdir()
    custom_client.write_text("from kirk.client import IrcClient\n\nclass MyClient(IrcClient):\n    pass\n")

    client_class = load_client_class(f"{custom_client}:MyClient", str(tmp_path / "kirk.toml"))

    assert issubclass(client_class, IrcClient)


def test_load_client_class_requires_colon_separator(tmp_path):
    with pytest.raises(ValueError, match="path/to/file.py:ClassName"):
        load_client_class("custom_client.py", str(tmp_path / "kirk.toml"))


def test_load_client_class_requires_class_name(tmp_path):
    with pytest.raises(ValueError, match="path/to/file.py:ClassName"):
        load_client_class("custom_client.py:", str(tmp_path / "kirk.toml"))
