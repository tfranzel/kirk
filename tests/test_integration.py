import asyncio
import base64
import ipaddress
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kirk.client import IrcClient, IrcRawMessage
from kirk.utils import resolve_dcc_path


def mock_writer() -> Mock:
    # write() is sync on a real StreamWriter, only drain() is a coroutine
    writer = Mock()
    writer.drain = AsyncMock()
    return writer


@pytest.mark.asyncio
async def test_message_flow_privmsg():
    client = IrcClient(host="test.com", nick="testnick")

    join_msg = IrcRawMessage("testnick!user@host", "JOIN", ["#testchan"])
    await client.process_message(join_msg)

    assert "#testchan" in client.channels
    assert "testnick" in client.channels["#testchan"].users

    privmsg = IrcRawMessage("othernick!user@host", "PRIVMSG", ["#testchan", "Hello everyone!"])
    await client.process_message(privmsg)

    channel_messages = list(client.channels["#testchan"].buf)
    assert any(msg.command == "PRIVMSG" and msg.params[1] == "Hello everyone!" for msg in channel_messages)


@pytest.mark.asyncio
async def test_message_flow_private_message():
    client = IrcClient(host="test.com", nick="testnick")

    privmsg = IrcRawMessage("friend!user@host", "PRIVMSG", ["testnick", "Hey there!"])
    await client.process_message(privmsg)

    chat_messages = list(client.chats["friend"])
    assert len(chat_messages) == 1
    assert chat_messages[0].params[1] == "Hey there!"


@pytest.mark.asyncio
async def test_channel_lifecycle():
    client = IrcClient(host="test.com", nick="testnick")

    await client.process_message(IrcRawMessage("testnick!user@host", "JOIN", ["#lifecycle"]))
    await client.process_message(IrcRawMessage("friend!user@host", "JOIN", ["#lifecycle"]))
    assert len(client.channels["#lifecycle"].users) == 2

    await client.process_message(IrcRawMessage("server.com", "332", ["testnick", "#lifecycle", "Test topic"]))
    assert client.channels["#lifecycle"].topic == "Test topic"

    await client.process_message(IrcRawMessage("friend!user@host", "PART", ["#lifecycle", "Goodbye"]))
    channel_messages = list(client.channels["#lifecycle"].buf)
    assert any(msg.command == "PART" for msg in channel_messages)


@pytest.mark.asyncio
async def test_user_quit_propagation():
    client = IrcClient(host="test.com", nick="testnick")

    channels = ["#chan1", "#chan2", "#chan3"]
    for chan in channels:
        await client.process_message(IrcRawMessage("quitter!user@host", "JOIN", [chan]))
        assert "quitter" in client.channels[chan].users

    await client.process_message(IrcRawMessage("quitter!user@host", "QUIT", ["Connection lost"]))

    for chan in channels:
        assert "quitter" not in client.channels[chan].users
        assert any(msg.command == "QUIT" for msg in client.channels[chan].buf)


@pytest.mark.asyncio
async def test_nick_change_handling():
    client = IrcClient(host="test.com", nick="oldnick")

    nick_msg = IrcRawMessage("oldnick!user@host", "NICK", ["newnick"])
    await client.process_message(nick_msg)

    assert client.nick == "newnick"


@pytest.mark.asyncio
async def test_mode_change_user():
    client = IrcClient(host="test.com", nick="testnick")

    await client.process_message(IrcRawMessage("server.com", "MODE", ["testnick", "+r"]))
    assert "r" in client.mode

    await client.process_message(IrcRawMessage("server.com", "MODE", ["testnick", "-r"]))
    assert "r" not in client.mode


@pytest.mark.asyncio
async def test_ctcp_version_response():
    client = IrcClient(host="test.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    ctcp_msg = IrcRawMessage("requester!user@host", "PRIVMSG", ["testnick", "\x01VERSION\x01"])
    await client.process_message(ctcp_msg)

    call_args = writer.write.call_args[0][0]
    assert b"NOTICE requester :\x01VERSION" in call_args
    assert client.version.encode() in call_args


@pytest.mark.asyncio
async def test_ctcp_ping_response():
    client = IrcClient(host="test.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    ping_data = "1234567890"
    ctcp_msg = IrcRawMessage("requester!user@host", "PRIVMSG", ["testnick", f"\x01PING {ping_data}\x01"])
    await client.process_message(ctcp_msg)

    call_args = writer.write.call_args[0][0]
    assert b"NOTICE requester :\x01PING" in call_args
    assert ping_data.encode() in call_args


@pytest.mark.asyncio
async def test_encrypted_message_flow():
    sender = IrcClient(host="test1.com", nick="alice", keys={"bob": "test_key"})
    receiver = IrcClient(host="test2.com", nick="bob", keys={"alice": "test_key"})

    writer = mock_writer()
    sender._writer = writer

    with patch("kirk.client.Fernet") as mock_fernet:
        mock_cipher = Mock()
        mock_fernet.return_value = mock_cipher
        mock_cipher.encrypt.return_value = b"encrypted_data"
        mock_cipher.decrypt.return_value = b"secret message"

        await sender.send_message("bob", "secret message", encrypt=True)
        mock_cipher.encrypt.assert_called_with(b"secret message")

        recv_msg = IrcRawMessage("alice!user@host", "PRIVMSG", ["bob", "~encrypted_data"])
        await receiver.process_message(recv_msg)

        chat_messages = list(receiver.chats["alice"])
        assert len(chat_messages) == 1
        assert chat_messages[0].secure
        assert chat_messages[0].params[1] == "secret message"


@pytest.mark.asyncio
async def test_dcc_offer_parsing():
    client = IrcClient(host="test.com", nick="testnick", dcc_dir="/tmp")

    dcc_text = "DCC SEND testfile.txt 3232235521 1234 1000"
    ctcp_msg = IrcRawMessage("sender!user@host", "PRIVMSG", ["testnick", f"\x01{dcc_text}\x01"])

    with patch.object(client, "_delay") as mock_delay:
        await client.process_message(ctcp_msg)

        assert len(client.dcc) == 1
        dcc = client.dcc[0]
        assert dcc.filename == "testfile.txt"
        assert dcc.ip == "192.168.0.1"  # 3232235521 as a dotted-quad
        assert dcc.port == 1234
        assert dcc.size == 1000
        mock_delay.assert_called_once()
        mock_delay.call_args[0][0].close()  # avoid "coroutine was never awaited" from the mock


def test_resolve_dcc_path_plain_filename(tmp_path: Path) -> None:
    resolved = resolve_dcc_path(str(tmp_path), "picture.png")
    assert resolved == tmp_path.resolve() / "picture.png"


def test_resolve_dcc_path_rejects_relative_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid DCC filename"):
        resolve_dcc_path(str(tmp_path), "../../../../etc/passwd")


def test_resolve_dcc_path_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid DCC filename"):
        resolve_dcc_path(str(tmp_path), "/etc/passwd")


def test_resolve_dcc_path_rejects_subdirectory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid DCC filename"):
        resolve_dcc_path(str(tmp_path), "subdir/file.txt")


def test_resolve_dcc_path_rejects_bare_traversal_segment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid DCC filename"):
        resolve_dcc_path(str(tmp_path), "..")


def test_resolve_dcc_path_rejects_empty_filename(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid DCC filename"):
        resolve_dcc_path(str(tmp_path), "")


@pytest.mark.asyncio
async def test_dcc_download_rejects_path_traversal_offer(tmp_path: Path) -> None:
    client = IrcClient(host="test.com", nick="testnick", dcc_dir=str(tmp_path))

    dcc_text = "DCC SEND ../../../../etc/passwd 3232235521 1234 1000"
    ctcp_msg = IrcRawMessage("sender!user@host", "PRIVMSG", ["testnick", f"\x01{dcc_text}\x01"])

    with patch.object(client, "_delay") as mock_delay:
        await client.process_message(ctcp_msg)
        dcc = client.dcc[0]
        mock_delay.call_args[0][0].close()  # avoid "coroutine was never awaited" from the mock

    with patch("asyncio.open_connection") as mock_open_connection:
        await client.dcc_download(dcc)

    # the malicious offer never even reaches the point of opening a connection
    mock_open_connection.assert_not_called()
    assert not (tmp_path.parent / "passwd").exists()
    assert list(tmp_path.iterdir()) == []


class _FakeServer:
    """Stand-in for asyncio.Server: supports `async with` without opening a real socket."""

    def __init__(self) -> None:
        self.close = Mock()

    async def __aenter__(self) -> "_FakeServer":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_dcc_send_offers_file(tmp_path: Path) -> None:
    file = tmp_path / "payload.bin"
    file.write_bytes(b"x" * 42)

    client = IrcClient(host="test.com", nick="testnick", dcc_host_ip="203.0.113.1")
    writer = mock_writer()
    client._writer = writer

    with patch("asyncio.start_server", AsyncMock(return_value=_FakeServer())) as mock_start_server:
        await client.dcc_send("receiver", file)

    mock_start_server.assert_called_once()
    _, host, port = mock_start_server.call_args[0]
    assert host == "0.0.0.0"
    assert mock_start_server.call_args.kwargs["backlog"] == 1

    call_args = writer.write.call_args[0][0]
    expected_host = int(ipaddress.ip_address("203.0.113.1"))
    assert f"DCC SEND payload.bin {expected_host} {port} 42".encode() in call_args


@pytest.mark.asyncio
async def test_dcc_send_requires_host_ip(tmp_path: Path) -> None:
    file = tmp_path / "payload.bin"
    file.write_bytes(b"x")

    client = IrcClient(host="test.com", nick="testnick")
    client.dcc_host_ip = ""  # force the "not configured" branch; the constructor defaults it

    with patch("asyncio.start_server") as mock_start_server:
        await client.dcc_send("receiver", file)

    mock_start_server.assert_not_called()


@pytest.mark.asyncio
async def test_dcc_send_missing_file(tmp_path: Path) -> None:
    client = IrcClient(host="test.com", nick="testnick", dcc_host_ip="203.0.113.1")

    with patch("asyncio.start_server") as mock_start_server:
        await client.dcc_send("receiver", tmp_path / "does-not-exist.bin")

    mock_start_server.assert_not_called()


@pytest.mark.asyncio
async def test_auth_flow_integration():
    client = IrcClient(host="test.com", nick="testnick", auth="nickserv", password="password123")
    writer = mock_writer()
    client._writer = writer

    # perform_nickserv_auth waits for client.mode to be non-empty before identifying
    await client.process_message(IrcRawMessage("server.com", "MODE", ["testnick", "+r"]))

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await client.perform_nickserv_auth()

        calls = [call[0][0] for call in writer.write.call_args_list]
        assert any(b"IDENTIFY testnick password123" in call for call in calls)


@pytest.mark.asyncio
async def test_sasl_plain_auth_flow_integration():
    client = IrcClient(host="test.com", nick="testnick", auth="sasl_plain", password="password123")
    writer = mock_writer()
    client._writer = writer

    # perform_auth polls with asyncio.sleep(1) between steps; patching "asyncio.sleep"
    # patches the module attribute everywhere (kirk.client imports the module itself,
    # not the function), so grab the real one first to avoid infinite self-recursion.
    real_sleep = asyncio.sleep

    async def instant_sleep(_seconds: float) -> None:
        await real_sleep(0)

    async def tick() -> None:
        for _ in range(3):
            await real_sleep(0)

    with patch("kirk.client.asyncio.sleep", instant_sleep):
        auth_task = asyncio.create_task(client.perform_sasl_auth())

        await tick()
        await client.process_message(IrcRawMessage(None, "CAP", ["testnick", "ACK", "sasl"]))
        await tick()
        await client.process_message(IrcRawMessage(None, "AUTHENTICATE", ["+"]))
        await tick()
        await client.process_message(
            IrcRawMessage(None, "903", ["testnick", "SASL authentication successful"])
        )
        await asyncio.wait_for(auth_task, timeout=1)

    calls = [call[0][0] for call in writer.write.call_args_list]
    assert any(b"AUTHENTICATE PLAIN" in call for call in calls)
    expected_payload = base64.b64encode(b"\0testnick\0password123").decode().encode()
    assert any(expected_payload in call for call in calls)
