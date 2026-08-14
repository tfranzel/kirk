from unittest.mock import Mock, patch

import pytest

from kirk.client import IrcClient, IrcRawMessage


@pytest.mark.asyncio
async def test_encryption_replay_protection():
    client = IrcClient(host="test.com", nick="testnick", keys={"friend": "test_key"})

    with patch("kirk.client.Fernet") as mock_fernet:
        mock_cipher = Mock()
        mock_fernet.return_value = mock_cipher
        mock_cipher.decrypt.return_value = b"secret message"

        encrypted_msg = IrcRawMessage("friend!user@host", "PRIVMSG", ["testnick", "~encrypted_data"])
        client.decrypt_privmsg(encrypted_msg)

        # decrypt must be called with a TTL, or old captured ciphertext could be replayed
        mock_cipher.decrypt.assert_called_with("encrypted_data", ttl=5)


def test_encryption_invalid_token_handling():
    client = IrcClient(host="test.com", nick="testnick", keys={"friend": "test_key"})

    with patch("kirk.client.Fernet") as mock_fernet:
        from cryptography.fernet import InvalidToken

        mock_cipher = Mock()
        mock_fernet.return_value = mock_cipher
        mock_cipher.decrypt.side_effect = InvalidToken()

        encrypted_msg = IrcRawMessage(
            "friend!user@host", "PRIVMSG", ["testnick", "~invalid_encrypted_data"]
        )

        # a forged/corrupted token must not raise out of message processing
        client.decrypt_privmsg(encrypted_msg)

        security_messages = [msg for msg in client.get_buf("friend") if msg.command == "SEC"]
        assert len(security_messages) > 0


def test_parse_raw_message_handles_arbitrary_bytes():
    # the hand-rolled parser must not crash on non-UTF8 / BOM'd input from the wire
    test_inputs = [
        b"\xff\xfe",
        b"\xef\xbb\xbf",
        b"\x80\x81\x82",
        b"caf\xe9",
    ]

    for test_input in test_inputs:
        msg = IrcClient.parse_raw_message(test_input + b"\r\n")
        assert isinstance(msg.command, str)
