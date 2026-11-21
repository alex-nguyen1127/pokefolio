"""Password hashing tests using synthetic password material."""

import unittest
from unittest.mock import patch

from argon2 import Type, extract_parameters
from argon2.exceptions import HashingError, UnsupportedParametersError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

from app.core.security import hash_password, verify_password


class PasswordSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "synthetic test password 🐉"
        cls.password_hash = hash_password(cls.password)

    def test_hash_uses_argon2id_profile(self):
        self.assertIsInstance(self.password_hash, str)
        self.assertNotEqual(self.password_hash, self.password)
        parameters = extract_parameters(self.password_hash)
        self.assertEqual(parameters.type, Type.ID)
        self.assertEqual(parameters, RFC_9106_LOW_MEMORY)

    def test_correct_password(self):
        self.assertTrue(verify_password(self.password, self.password_hash))

    def test_incorrect_password(self):
        self.assertFalse(verify_password("incorrect synthetic password", self.password_hash))

    def test_unique_salts(self):
        another_hash = hash_password(self.password)
        self.assertNotEqual(another_hash, self.password_hash)
        self.assertNotEqual(another_hash.split("$")[4], self.password_hash.split("$")[4])
        self.assertTrue(verify_password(self.password, another_hash))

    def test_malformed_and_unsupported_hashes(self):
        for value in ("", "invalid", "$2b$unsupported", "$argon2id$",
                      self.password_hash.replace("v=19", "v=999"),
                      self.password_hash.replace("m=65536", "m=invalid")):
            with self.subTest():
                self.assertFalse(verify_password(self.password, value))

    def test_library_verification_errors_fail_closed(self):
        for error in (VerificationError, UnsupportedParametersError):
            with self.subTest(error=error.__name__):
                with patch("app.core.security._password_hasher") as hasher:
                    hasher.verify.side_effect = error("synthetic failure")
                    self.assertFalse(verify_password(self.password, self.password_hash))

    def test_unexpected_operational_errors_propagate(self):
        with patch("app.core.security._password_hasher") as hasher:
            hasher.verify.side_effect = RuntimeError("synthetic operational failure")
            with self.assertRaises(RuntimeError):
                verify_password(self.password, self.password_hash)
            hasher.hash.side_effect = HashingError("synthetic hashing failure")
            with self.assertRaises(HashingError):
                hash_password(self.password)
