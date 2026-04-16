"""
edgebot/security/network.py - SSRF protection.

Blocks tool calls that target private/internal IP addresses.
All stdlib — no extra dependencies.
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+")


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
        return any(ip in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        return False


def validate_url_target(url: str) -> str | None:
    """
    Resolve *url* and check all resulting IPs against blocked networks.
    Returns an error message string if blocked, None if safe.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked: scheme '{parsed.scheme}' is not allowed"
    hostname = parsed.hostname
    if not hostname:
        return "Blocked: no hostname in URL"
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        return f"Blocked: DNS resolution failed — {e}"
    for _, _, _, _, sockaddr in results:
        ip_str = sockaddr[0]
        if _is_blocked_ip(ip_str):
            return f"Blocked: '{hostname}' resolves to internal IP {ip_str}"
    return None


def contains_internal_url(command: str) -> bool:
    """
    Scan a shell command string for any URL that points to an internal host.
    Returns True if a blocked URL is found.
    """
    for match in _URL_RE.finditer(command):
        url = match.group(0)
        if validate_url_target(url) is not None:
            return True
    return False
