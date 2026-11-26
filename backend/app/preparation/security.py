from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit


class UnsafeDestination(ValueError):
    pass


def extract_https_origin(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except (ValueError, AttributeError) as exc:
        raise UnsafeDestination("Application URL is malformed.") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeDestination("Only public HTTPS application URLs are supported.")
    if port not in (None, 443):
        raise UnsafeDestination("Application URLs must use the standard HTTPS port.")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    if host in {"localhost", "localhost.localdomain"} or "." not in host or host.endswith((".local", ".internal", ".test", ".invalid")):
        raise UnsafeDestination("Local and non-public hostnames are blocked.")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}"


def resolve_public_addresses(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url.strip())
    if not parsed.hostname:
        raise UnsafeDestination("Application URL has no host.")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except (OSError, ValueError) as exc:
            raise UnsafeDestination("Could not verify that the application host is publicly routable.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeDestination("Private, reserved, or local network destinations are blocked.")
    return host, [str(address) for address in addresses]


def validate_public_https_url(url: str) -> tuple[str, str]:
    """Return canonical (origin, URL) after rejecting unsafe network targets."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(ch) < 32 for ch in url):
        raise UnsafeDestination("Application URL is invalid or too long.")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise UnsafeDestination("Application URL is malformed.") from exc
    origin = extract_https_origin(url)
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    host, _ = resolve_public_addresses(url)
    path = parsed.path or "/"
    sensitive_query_names = {"token", "access_token", "refresh_token", "auth", "authorization", "password", "secret", "api_key", "session", "sessionid", "jwt", "code", "state", "sig", "signature"}
    safe_query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                  if key.casefold() not in sensitive_query_names and not re.search(r"token|secret|password|auth|session|csrf|xsrf|jwt|signature|^code$|^state$", key, re.I)]
    safe_url = origin + path + (f"?{urlencode(safe_query)}" if safe_query else "")
    return origin, safe_url
