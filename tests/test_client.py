from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kirk.client import (
    DCC,
    Buffer,
    ChannelDict,
    IrcChannel,
    IrcClient,
    IrcRawMessage,
)


def mock_writer() -> Mock:
    # write() is sync on a real StreamWriter, only drain() is a coroutine
    writer = Mock()
    writer.drain = AsyncMock()
    return writer


def test_irc_client_initialization():
    client = IrcClient(host="irc.example.com", nick="testnick")

    assert client.host == "irc.example.com"
    assert client.nick == "testnick"
    assert client.port == 6697
    assert client.ssl
    assert client.auto_join == []
    assert client.keys == {}
    assert client.log_mode == "none"


def test_irc_client_no_ssl_initialization():
    client = IrcClient(host="irc.example.com", nick="testnick", ssl=False)
    assert client.port == 6667
    assert not client.ssl


def test_irc_client_custom_port():
    client = IrcClient(host="irc.example.com", nick="testnick", port=6660)
    assert client.port == 6660


def test_get_buf():
    client = IrcClient(host="irc.example.com", nick="testnick")

    assert client.get_buf("irc.example.com") is client.server_buf
    assert client.get_buf("#testchannel") is client.channels["#testchannel"].buf
    assert client.get_buf("testnick") is client.chats["testnick"]


def test_irc_raw_message_creation():
    msg = IrcRawMessage("nick!user@host", "PRIVMSG", ["#channel", "hello"])
    assert msg.prefix == "nick!user@host"
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#channel", "hello"]
    assert not msg.secure
    assert isinstance(msg.ts, datetime)


def test_prefix_nick_variations():
    assert IrcRawMessage("nick!user@host", "X", []).prefix_nick == "nick"
    assert IrcRawMessage("nick!user@host", "X", []).prefix_host == "user@host"

    # no hostmask, e.g. server-originated messages
    server_msg = IrcRawMessage("server.example.com", "001", ["nick", "Welcome"])
    assert server_msg.prefix_nick == "server.example.com"
    assert server_msg.prefix_host == ""

    no_prefix_msg = IrcRawMessage(None, "PING", ["server.example.com"])
    assert no_prefix_msg.prefix_nick == ""
    assert no_prefix_msg.prefix_host == ""


def test_irc_channel_creation():
    channel = IrcChannel("#testchannel")

    assert channel.name == "#testchannel"
    assert channel.mode == ""
    assert channel.topic == ""
    assert channel.topic_origin == ("", "")
    assert channel.users == {}
    assert isinstance(channel.buf, Buffer)


def test_irc_channel_custom_buffer_size():
    channel = IrcChannel("#testchannel", buf_size=100)
    assert channel.buf.size == 100


def test_channel_dict():
    channels = ChannelDict()

    channel = channels["#newchannel"]
    assert isinstance(channel, IrcChannel)
    assert channel.name == "#newchannel"
    assert "#newchannel" in channels


def test_is_channel_name():
    assert IrcClient.is_channel_name("#channel")
    assert IrcClient.is_channel_name("&channel")
    assert IrcClient.is_channel_name("+channel")
    assert IrcClient.is_channel_name("!channel")
    assert not IrcClient.is_channel_name("nickname")
    assert not IrcClient.is_channel_name("server.com")


def test_dcc_creation():
    start_time = datetime.now()
    dcc = DCC(
        source="sender",
        filename="test.txt",
        size=1024,
        ip="192.168.1.1",
        port=1234,
        start_time=start_time,
    )

    assert dcc.source == "sender"
    assert dcc.filename == "test.txt"
    assert dcc.size == 1024
    assert dcc.end_time is None
    assert not dcc.ssl
    assert dcc.bytes_received == 0
    assert not dcc.verified


def test_dcc_complete():
    dcc = DCC("sender", "test.txt", 100, "127.0.0.1", 1234, datetime.now())

    assert not dcc.complete

    dcc.bytes_received = 50
    assert not dcc.complete

    dcc.bytes_received = 100
    assert dcc.complete


def test_parse_simple_message():
    raw = b":nick!user@host PRIVMSG #channel :Hello world\r\n"
    msg = IrcClient.parse_raw_message(raw)

    assert msg.prefix == "nick!user@host"
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#channel", "Hello world"]


def test_parse_message_no_prefix():
    raw = b"PING :server.example.com\r\n"
    msg = IrcClient.parse_raw_message(raw)

    assert msg.prefix is None
    assert msg.command == "PING"
    assert msg.params == ["server.example.com"]


def test_parse_message_no_trailing():
    raw = b":nick!user@host JOIN #channel\r\n"
    msg = IrcClient.parse_raw_message(raw)

    assert msg.command == "JOIN"
    assert msg.params == ["#channel"]


def test_parse_message_multiple_params():
    raw = b":server.com 353 nick = #channel :nick1 nick2 nick3\r\n"
    msg = IrcClient.parse_raw_message(raw)

    assert msg.command == "353"
    assert msg.params == ["nick", "=", "#channel", "nick1 nick2 nick3"]


def test_parse_malformed_message():
    # a single word with no prefix/params is treated as the command
    msg = IrcClient.parse_raw_message(b"MALFORMED\r\n")
    assert msg.prefix is None
    assert msg.command == "MALFORMED"
    assert msg.params == []


def test_parse_empty_message():
    msg = IrcClient.parse_raw_message(b"\r\n")
    assert msg.command == ""
    assert msg.params == []


def test_parse_unicode_message():
    raw = "test café 🔥".encode() + b"\r\n"
    msg = IrcClient.parse_raw_message(raw)

    assert msg.command == "test"
    assert msg.params == ["café", "🔥"]


@pytest.mark.asyncio
async def test_send_cmd_simple():
    client = IrcClient(host="irc.example.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    await client.send_cmd("PING", "server.example.com")

    writer.write.assert_called_once_with(b"PING server.example.com\r\n")
    writer.drain.assert_called_once()


@pytest.mark.asyncio
async def test_send_cmd_with_trailing():
    client = IrcClient(host="irc.example.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    await client.send_cmd("PRIVMSG", "#channel", "Hello world")

    writer.write.assert_called_once_with(b"PRIVMSG #channel :Hello world\r\n")


@pytest.mark.asyncio
async def test_send_cmd_with_params_list():
    client = IrcClient(host="irc.example.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    await client.send_cmd("MODE", ["#channel", "+o", "nick"])

    writer.write.assert_called_once_with(b"MODE #channel +o nick\r\n")


@pytest.mark.asyncio
async def test_join_channel():
    client = IrcClient(host="irc.example.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    await client.join_channel("#testchannel")

    writer.write.assert_called_once_with(b"JOIN #testchannel\r\n")


@pytest.mark.asyncio
async def test_send_message_plain():
    client = IrcClient(host="irc.example.com", nick="testnick")
    writer = mock_writer()
    client._writer = writer

    await client.send_message("#channel", "Hello world")

    writer.write.assert_called_once_with(b"PRIVMSG #channel :Hello world\r\n")
    messages = list(client.get_buf("#channel"))
    assert len(messages) == 1
    assert messages[0].params[1] == "Hello world"


@pytest.mark.asyncio
async def test_send_message_encrypted():
    client = IrcClient(host="irc.example.com", nick="testnick", keys={"friend": "encryption_key"})
    writer = mock_writer()
    client._writer = writer

    with patch("kirk.client.Fernet") as mock_fernet:
        mock_cipher = Mock()
        mock_fernet.return_value = mock_cipher
        mock_cipher.encrypt.return_value = b"encrypted_data"

        await client.send_message("friend", "secret message", encrypt=True)

        mock_fernet.assert_called_once_with("encryption_key")
        mock_cipher.encrypt.assert_called_once_with(b"secret message")

        call_args = writer.write.call_args[0][0]
        assert b"~encrypted_data" in call_args


@pytest.mark.asyncio
async def test_delete_channel():
    client = IrcClient(host="test.com", nick="test")
    writer = mock_writer()
    client._writer = writer

    client.channels["#test"]
    assert "#test" in client.channels

    await client.delete("#test")

    writer.write.assert_called_once_with(b"PART #test\r\n")
    assert "#test" not in client.channels


@pytest.mark.asyncio
async def test_delete_chat():
    client = IrcClient(host="test.com", nick="test")

    client.chats["friend"].insert(IrcRawMessage("friend", "PRIVMSG", ["test", "hi"]))
    assert "friend" in client.chats

    await client.delete("friend")

    assert "friend" not in client.chats
