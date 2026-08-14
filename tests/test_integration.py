from unittest.mock import AsyncMock, Mock, patch

import pytest

from kirk.client import IrcClient, IrcRawMessage


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
    assert any(
        msg.command == "PRIVMSG" and msg.params[1] == "Hello everyone!" for msg in channel_messages
    )


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

    await client.process_message(
        IrcRawMessage("server.com", "332", ["testnick", "#lifecycle", "Test topic"])
    )
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


@pytest.mark.asyncio
async def test_auth_flow_integration():
    client = IrcClient(host="test.com", nick="testnick", auth="password123")
    writer = mock_writer()
    client._writer = writer

    # perform_auth waits for client.mode to be non-empty before identifying
    await client.process_message(IrcRawMessage("server.com", "MODE", ["testnick", "+r"]))

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await client.perform_auth()

        calls = [call[0][0] for call in writer.write.call_args_list]
        assert any(b"IDENTIFY testnick password123" in call for call in calls)
