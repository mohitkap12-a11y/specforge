"""Offline mode backstop: refuse outbound connections from this process to public internet addresses.

Loopback, private (LAN), link-local and Unix-socket connections are still allowed, so a local or on-premises AI server
(Ollama on 127.0.0.1 or another machine on the LAN) keeps working. Only Python sockets in this server process are
covered: a separate program it starts (such as the OpenCode CLI) has its own network access.
"""
from __future__ import annotations

import collections
import errno
import ipaddress
import logging
import socket
import threading
import time

_log = logging.getLogger("specforge.netguard")
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_enabled = False
_lock = threading.Lock()
blocked: collections.deque = collections.deque(maxlen=20)   # (time, "host:port") of the latest refused connections
blocked_total = 0


class OfflineBlocked(ConnectionRefusedError):
    pass


def _is_public(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True          # not an address: decided by _target_public after resolving it
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def _target_public(sock: socket.socket, address) -> str | None:
    """'host:port' when the address is on the public internet, else None."""
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple) or not address:
        return None
    host, port = str(address[0]), address[1] if len(address) > 1 else 0
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addrs = [host]
    except ValueError:      # a host name: every address it resolves to must be local
        try:
            addrs = [ai[4][0] for ai in socket.getaddrinfo(host, port, sock.family)]
        except OSError:
            return None     # cannot resolve: the real connect fails the same way
    return f"{host}:{port}" if any(_is_public(a) for a in addrs) else None


def _refuse(target: str) -> OfflineBlocked:
    global blocked_total
    with _lock:
        blocked.append((time.time(), target))
        blocked_total += 1
    _log.warning("offline mode: blocked a connection to %s", target)
    return OfflineBlocked(errno.ECONNREFUSED, f"SpecForge offline mode blocked a connection to {target} "
                                              "(turn Offline off in Settings to allow internet access)")


def _connect(self, address):
    if _enabled and (t := _target_public(self, address)):
        raise _refuse(t)
    return _orig_connect(self, address)


def _connect_ex(self, address):
    if _enabled and (t := _target_public(self, address)):
        _refuse(t)
        return errno.ECONNREFUSED
    return _orig_connect_ex(self, address)


def enable() -> None:
    global _enabled
    socket.socket.connect = _connect          # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex    # type: ignore[method-assign]
    _enabled = True


def disable() -> None:
    global _enabled
    _enabled = False
    socket.socket.connect = _orig_connect     # type: ignore[method-assign]
    socket.socket.connect_ex = _orig_connect_ex  # type: ignore[method-assign]


def enabled() -> bool:
    return _enabled


def status() -> dict:
    with _lock:
        return {"active": _enabled, "blocked_total": blocked_total,
                "recent": [{"time": t, "target": tg} for t, tg in reversed(blocked)]}
