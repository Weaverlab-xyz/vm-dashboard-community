"""Where the dashboard may send an outbound request whose URL a user typed.

A webhook URL is chosen by whoever holds ``notifications:write`` (a delegable grant), and
the dashboard POSTs to it from inside the network it runs in. Unchecked, that makes it a
proxy onto anything it can reach: the cloud metadata service (instance credentials),
localhost admin ports, internal services. So the host is resolved and every address it
resolves to is checked, both when the URL is saved and again at send time -- a name can
resolve differently later (DNS rebinding), and rows saved before this check existed must
not keep working past it.

Always refused: loopback, link-local (169.254.0.0/16 holds AWS/GCP/Azure/OCI metadata;
fe80::/10 and the EC2 IPv6 metadata address too), unspecified, multicast and reserved.
Private and shared ranges (RFC 1918, 100.64.0.0/10, IPv6 ULA) are refused too unless an
administrator turns on ``notifications_allow_private_targets``
(read by ``notify_policy.allow_private_targets``), because an internal alert
receiver is a legitimate thing to point a custom webhook at.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

# fd00:ec2::254 is the EC2 instance metadata service over IPv6. It sits inside the ULA
# range, so the private-range opt-in alone would let it through.
_ALWAYS_BLOCKED_ADDRS = {ipaddress.ip_address("fd00:ec2::254")}


def _resolve(host: str, port: int):
    """The one DNS lookup; tests replace it."""
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


class OutboundURLRefused(ValueError):
    """The URL is not one the dashboard may send to; the message says why."""


def _classify(addr) -> str:
    """'' when the address is public, else the reason it is not."""
    if addr in _ALWAYS_BLOCKED_ADDRS:
        return "a cloud metadata address"
    if addr.is_loopback:
        return "a loopback address"
    if addr.is_link_local:
        return "a link-local address (which includes cloud metadata services)"
    if addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        return "not a routable unicast address"
    if addr.is_private or addr in ipaddress.ip_network("100.64.0.0/10"):
        return "private"
    return ""


def check_webhook_url(url: str, *, allow_private: bool = False) -> None:
    """Raise :class:`OutboundURLRefused` unless every address ``url`` resolves to is allowed.

    A name that does not resolve is refused as well: there is nothing to check, and the
    send would fail anyway.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OutboundURLRefused("The webhook URL must be an http:// or https:// URL with a host.")
    host = parts.hostname
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise OutboundURLRefused(f"The webhook URL has an invalid port: {exc}") from exc
    try:
        infos = _resolve(host, port)
    except (socket.gaierror, UnicodeError) as exc:
        raise OutboundURLRefused(f"The webhook host {host!r} does not resolve.") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        why = _classify(addr)
        if why == "private" and allow_private:
            continue
        if why == "private":
            raise OutboundURLRefused(
                f"The webhook host {host!r} resolves to a private address ({addr}). An "
                "administrator can allow internal webhook targets on the Notifications "
                "settings panel.")
        if why:
            raise OutboundURLRefused(
                f"The webhook host {host!r} resolves to {addr}, {why}; the dashboard will "
                "not send to it.")
