import base64
from unittest.mock import AsyncMock, Mock, patch

import pytest
import tomlkit

from kirk.client import IrcClient, IrcRawMessage
from kirk.security import KeyExchange


def mock_writer() -> Mock:
    # write() is sync on a real StreamWriter, only drain() is a coroutine
    writer = Mock()
    writer.drain = AsyncMock()
    return writer


def extract_ctcp_text(writer: Mock) -> str:
    """Pull the CTCP payload out of the last raw line written to `writer`."""
    raw = str(writer.write.call_args[0][0].decode())
    _, _, trailing = raw.partition(" :")
    return trailing.strip("\r\n").strip("\x01")


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

        encrypted_msg = IrcRawMessage("friend!user@host", "PRIVMSG", ["testnick", "~invalid_encrypted_data"])

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


def test_key_exchange_generates_32_byte_salt():
    assert len(base64.b64decode(KeyExchange().salt)) == 32


def test_key_exchange_requires_both_peer_key_and_salt_together():
    kx = KeyExchange()
    with pytest.raises(ValueError):
        KeyExchange(peer_public_key=kx.public_key)
    with pytest.raises(ValueError):
        KeyExchange(salt=kx.salt)


def test_dh_key_exchange_derives_matching_key():
    alice = KeyExchange()
    bob = KeyExchange(alice.public_key, alice.salt)
    alice.set_peer_public_key(bob.public_key)

    assert alice.shared_key == bob.shared_key
    assert alice.fingerprint == bob.fingerprint


def test_dh_key_exchange_independent_handshakes_yield_different_keys():
    alice1 = KeyExchange()
    bob1 = KeyExchange(alice1.public_key, alice1.salt)

    alice2 = KeyExchange()
    bob2 = KeyExchange(alice2.public_key, alice2.salt)

    assert bob1.shared_key != bob2.shared_key


def test_fingerprint_is_deterministic_and_uses_every_4th_byte():
    alice = KeyExchange()
    bob = KeyExchange(alice.public_key, alice.salt)

    words = bob.fingerprint.split()
    assert bob.fingerprint == bob.fingerprint
    assert len(words) == len(base64.urlsafe_b64decode(bob.shared_key)[::4])


def test_fingerprint_differs_for_different_keys():
    alice1 = KeyExchange()
    bob1 = KeyExchange(alice1.public_key, alice1.salt)

    alice2 = KeyExchange()
    bob2 = KeyExchange(alice2.public_key, alice2.salt)

    assert bob1.fingerprint != bob2.fingerprint


@pytest.mark.asyncio
async def test_dh_handshake_end_to_end_over_ctcp():
    """Full DH1/DH2 handshake between two real IrcClient instances, no crypto mocking."""
    alice = IrcClient(host="test1.com", nick="alice")
    bob = IrcClient(host="test2.com", nick="bob")
    alice._writer = mock_writer()
    bob._writer = mock_writer()

    await alice.start_key_exchange("bob")
    assert "bob" in alice._dh_pending

    dh1_text = extract_ctcp_text(alice._writer)
    assert dh1_text.startswith("DH1 ")
    await bob.process_message(IrcRawMessage("alice!user@host", "PRIVMSG", ["bob", f"\x01{dh1_text}\x01"]))

    assert "alice" in bob.keys

    dh2_text = extract_ctcp_text(bob._writer)
    assert dh2_text.startswith("DH2 ")
    await alice.process_message(IrcRawMessage("bob!user@host", "PRIVMSG", ["alice", f"\x01{dh2_text}\x01"]))

    assert "bob" in alice.keys
    assert "bob" not in alice._dh_pending
    assert alice.keys["bob"] == bob.keys["alice"]


@pytest.mark.asyncio
async def test_dh_handshake_persists_key_to_config(tmp_path):
    config_path = tmp_path / "kirk.toml"
    config_path.write_text('[kirk]\n\n[[kirk.client]]\nhost = "test1.com"\nnick = "alice"\n')

    alice = IrcClient(host="test1.com", nick="alice", config_path=str(config_path))
    bob = IrcClient(host="test2.com", nick="bob")
    alice._writer = mock_writer()
    bob._writer = mock_writer()

    await alice.start_key_exchange("bob")
    dh1_text = extract_ctcp_text(alice._writer)
    await bob.process_message(IrcRawMessage("alice!user@host", "PRIVMSG", ["bob", f"\x01{dh1_text}\x01"]))
    dh2_text = extract_ctcp_text(bob._writer)
    await alice.process_message(IrcRawMessage("bob!user@host", "PRIVMSG", ["alice", f"\x01{dh2_text}\x01"]))

    doc = tomlkit.parse(config_path.read_text())
    assert doc["kirk"]["client"][0]["keys"]["bob"] == alice.keys["bob"]


@pytest.mark.asyncio
async def test_dh_handshake_rejects_unsolicited_reply():
    alice = IrcClient(host="test.com", nick="alice")

    await alice.process_ctcp_dh2("mallory", f"DH2 {KeyExchange().public_key}")

    assert "mallory" not in alice.keys


@pytest.mark.asyncio
async def test_dh_handshake_does_not_overwrite_existing_key_on_dh1():
    # an attacker resending/forging a DH1 must not be able to silently swap out an
    # already-established key
    alice = IrcClient(host="test.com", nick="alice", keys={"mallory": "trusted_key"})
    alice._writer = mock_writer()

    rogue = KeyExchange()
    rogue_dh1 = f"DH1 {rogue.public_key} {rogue.salt}"
    await alice.process_message(
        IrcRawMessage("mallory!user@host", "PRIVMSG", ["alice", f"\x01{rogue_dh1}\x01"])
    )

    assert alice.keys["mallory"] == "trusted_key"
    alice._writer.write.assert_not_called()


@pytest.mark.asyncio
async def test_dh_handshake_does_not_overwrite_existing_key_on_dh2():
    # a forged DH2 reply must not be able to overwrite a key that was established in
    # the meantime (e.g. concurrently via config or another handshake)
    alice = IrcClient(host="test.com", nick="alice")
    alice._writer = mock_writer()

    await alice.start_key_exchange("bob")
    assert "bob" in alice._dh_pending

    alice.keys["bob"] = "trusted_key"
    rogue_dh2 = f"DH2 {KeyExchange().public_key}"
    await alice.process_message(IrcRawMessage("bob!user@host", "PRIVMSG", ["alice", f"\x01{rogue_dh2}\x01"]))

    assert alice.keys["bob"] == "trusted_key"
    assert "bob" not in alice._dh_pending


@pytest.mark.asyncio
async def test_start_key_exchange_refuses_when_key_already_exists():
    alice = IrcClient(host="test.com", nick="alice", keys={"bob": "trusted_key"})
    alice._writer = mock_writer()

    await alice.start_key_exchange("bob")

    assert "bob" not in alice._dh_pending
    alice._writer.write.assert_not_called()


@pytest.mark.asyncio
async def test_start_key_exchange_does_not_clobber_pending_exchange():
    # a second call before bob replies must not replace the in-flight private key,
    # or bob's eventual DH2 reply would derive a mismatched secret
    alice = IrcClient(host="test.com", nick="alice")
    alice._writer = mock_writer()

    await alice.start_key_exchange("bob")
    pending = alice._dh_pending["bob"]

    await alice.start_key_exchange("bob")

    assert alice._dh_pending["bob"] is pending
    assert alice._writer.write.call_count == 1
