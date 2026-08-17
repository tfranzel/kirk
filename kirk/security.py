import base64
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_WORDLIST = [
    "alert",
    "alien",
    "antenna",
    "atom",
    "biology",
    "bridge",
    "captain",
    "code",
    "core",
    "crater",
    "crew",
    "cruise",
    "crystal",
    "desert",
    "device",
    "discover",
    "element",
    "energy",
    "engine",
    "galaxy",
    "gravity",
    "hybrid",
    "ice",
    "impulse",
    "journey",
    "light",
    "logic",
    "lunar",
    "machine",
    "matrix",
    "monitor",
    "moon",
    "mystery",
    "network",
    "nuclear",
    "observe",
    "ocean",
    "orbit",
    "oxygen",
    "panel",
    "pilot",
    "pioneer",
    "planet",
    "power",
    "program",
    "quantum",
    "radar",
    "robot",
    "rocket",
    "scan",
    "security",
    "shield",
    "solar",
    "space",
    "speed",
    "sun",
    "travel",
    "universe",
    "unknown",
    "vacuum",
    "vessel",
    "void",
    "volcano",
    "voyage",
]


class KeyExchange:
    """
    Diffie-Hellman key exchange for securing private conversations between two users.

    Handshake, driven over two CTCP requests (see `process_privmsg_ctcp` in client.py). Both
    messages are sent as CTCP requests (PRIVMSG), not NOTICE replies, so each side's regular
    incoming-PRIVMSG/CTCP pipeline picks up its counterpart:
        initiator -> peer:  DH1 <initiator_pubkey_b64> <salt_b64>
        peer -> initiator:  DH2 <peer_pubkey_b64>

    Both sides then hold an X25519 shared secret, which is run through HKDF (bound to the
    salt chosen by the initiator) to derive a Fernet key; see `fingerprint` for verifying
    it out-of-band against a man-in-the-middle.
    """

    _private_key: X25519PrivateKey
    _salt: bytes
    _peer_public_key: X25519PublicKey | None = None
    _derived_secret: bytes | None = None

    def __init__(self, peer_public_key: str | None = None, salt: str | None = None):
        self._private_key = X25519PrivateKey.generate()

        if peer_public_key and salt:
            self._salt = base64.b64decode(salt)
            self._peer_public_key = X25519PublicKey.from_public_bytes(base64.b64decode(peer_public_key))
            self._derive_fernet_key()
        elif peer_public_key or salt:
            raise ValueError()
        else:
            self._salt = secrets.token_bytes(32)

    def set_peer_public_key(self, peer_public_key: str) -> None:
        self._peer_public_key = X25519PublicKey.from_public_bytes(base64.b64decode(peer_public_key))
        self._derive_fernet_key()

    @property
    def public_key(self) -> str:
        raw = self._private_key.public_key().public_bytes_raw()
        return base64.b64encode(raw).decode()

    @property
    def salt(self) -> str:
        return base64.b64encode(self._salt).decode()

    @property
    def shared_key(self) -> str:
        assert self._derived_secret is not None, "peer public key not set yet"
        return base64.urlsafe_b64encode(self._derived_secret).decode()

    @property
    def fingerprint(self) -> str:
        """
        Derive a short, human-verifiable mnemonic from a derived key to rule out
        a man-in-the-middle. Only every 4th byte is used to keep the phrase short.
        """
        assert self._derived_secret is not None, "peer public key not set yet"
        return " ".join(_WORDLIST[b % len(_WORDLIST)] for b in self._derived_secret[::4])

    def _derive_fernet_key(self) -> None:
        """Run the DH shared secret through HKDF and return a Fernet-ready key."""
        assert self._peer_public_key is not None
        shared_secret = self._private_key.exchange(self._peer_public_key)
        self._derived_secret = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self._salt,
            info=None,
        ).derive(shared_secret)
