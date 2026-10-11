"""Environment-backed configuration for the Spendstack MCP adapter."""

from dataclasses import dataclass
import ipaddress
import os
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when required MCP server configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    api_url: str
    api_token: str
    timeout_seconds: float = 15.0


def load_settings(environ=None):
    env = os.environ if environ is None else environ
    raw_url = env.get("SPENDSTACK_API_URL", "").strip()
    token = env.get("SPENDSTACK_API_TOKEN", "").strip()
    if not raw_url:
        raise ConfigurationError("SPENDSTACK_API_URL must be set.")
    if not token:
        raise ConfigurationError("SPENDSTACK_API_TOKEN must be set.")
    if any(char.isspace() for char in raw_url):
        raise ConfigurationError("SPENDSTACK_API_URL must be a valid absolute HTTP(S) URL.")
    if any(char.isspace() for char in token) or len(token) > 8192:
        raise ConfigurationError("SPENDSTACK_API_TOKEN must be a single token of at most 8192 characters.")

    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        raise ConfigurationError("SPENDSTACK_API_URL must be a valid absolute HTTP(S) URL.") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigurationError("SPENDSTACK_API_URL must be a valid absolute HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ConfigurationError("SPENDSTACK_API_URL must not include credentials, a query, or a fragment.")
    host = parsed.hostname.rstrip(".").lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost" or host.endswith(".localhost")
    if parsed.scheme != "https" and not loopback:
        raise ConfigurationError("SPENDSTACK_API_URL must use HTTPS except for a loopback development server.")

    base_url = raw_url.rstrip("/")
    return Settings(api_url=base_url, api_token=token)