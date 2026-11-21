"""Password hashing without logging password material.

Uses argon2-cffi's RFC 9106 low-memory profile: Argon2id, 64 MiB,
three iterations, four lanes, a 16-byte random salt, and a 32-byte hash.
See https://argon2-cffi.readthedocs.io/en/stable/api.html#profiles
"""

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    UnsupportedParametersError,
    VerificationError,
)
from argon2.profiles import RFC_9106_LOW_MEMORY

_password_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)


def hash_password(password: str) -> str:
    """Hash a password with a fresh, library-generated salt.

    Hashing failures propagate so callers cannot persist a failed result.
    """
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether the password matches the stored encoded Argon2 hash.

    Mismatches, malformed hashes, and unsupported parameters fail closed.
    Unexpected operational exceptions propagate for server-side handling.
    Stored hashes must come from the database, never directly from a client.
    """
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, UnsupportedParametersError):
        return False
