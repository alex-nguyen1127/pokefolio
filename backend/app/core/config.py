"""Secure-by-default session cookie configuration."""

import ipaddress
import os
import re
from urllib.parse import urlsplit

SESSION_COOKIE_NAME = "pokefolio_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60


def session_cookie_secure(hostname: str | None) -> bool:
    """Allow HTTP cookies only with explicit development settings on loopback."""
    environment = os.getenv("POKEFOLIO_ENV", "production")
    setting = os.getenv("POKEFOLIO_SESSION_COOKIE_SECURE", "true")
    if environment not in {"production", "development"} or setting not in {"true", "false"}:
        raise ValueError("Invalid session cookie configuration")
    if setting == "true":
        return True
    if environment != "development" or hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Insecure cookies require local development")
    return False


def parse_origin(value: str) -> tuple[str, str, int]:
    """Parse one serialized HTTP(S) origin, rejecting URL components and lists."""
    if not value.isascii() or not re.fullmatch(
        r"https?://(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]+)?", value, re.IGNORECASE,
    ):
        raise ValueError("Invalid origin")
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid origin")
    if ":" in hostname:
        hostname = ipaddress.IPv6Address(hostname).compressed
    elif len(hostname) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in hostname.split(".")
    ):
        raise ValueError("Invalid origin")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid origin")
    return parsed.scheme, hostname, port


def trusted_frontend_origins() -> frozenset[tuple[str, str, int]]:
    """Read an explicit comma-separated allowlist; an empty list trusts nothing."""
    configured = os.getenv("POKEFOLIO_TRUSTED_ORIGINS", "")
    if not configured:
        return frozenset()
    return frozenset(parse_origin(value.strip()) for value in configured.split(","))
