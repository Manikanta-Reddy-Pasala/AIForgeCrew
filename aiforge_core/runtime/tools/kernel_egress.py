"""The egress policy, enforced INSIDE the notebook kernel.

The report this exists for, from a live session: the user asked the agent to
fetch youtube.com. ``web_fetch`` refused it — the host is not on the operator's
allowlist — and the agent immediately reran the same request as a shell
``curl``, and when that was refused too, as a notebook cell using an HTTP
library. That one worked. Every layer had answered honestly and the request
still got out, because each layer only knew about its own transport.

A refusal that can be walked around by changing transport is not a boundary,
and the operator had been told it was one. So the kernel — which we start, in
our own virtualenv, and can therefore reach into — now asks the same question
the fetch tools ask, at the two places a Python program can name a destination:

* ``socket.getaddrinfo`` sees the HOSTNAME. That is where the policy can be
  applied meaningfully: ``egress.check`` refuses a host that is not on the
  allowlist, a search engine, or anything at all while fetching is switched
  off.
* ``socket.socket.connect`` sees only an ADDRESS. A public IP that did not come
  out of a resolution we just allowed is refused — that is what stops
  ``socket.connect(("142.250.0.1", 443))`` and the libraries that pre-resolve.

Loopback and the private LAN pass, exactly as they do everywhere else: a dev
server on this machine is not egress, and taking it away would break ui_check
for no security gain.

**What this is NOT.** It is not a sandbox. Code running in the kernel can undo
these patches — it is the same process, and Python has no way to stop that. It
raises the cost of the reroute from "use a different library" to "deliberately
disable the guard", and it makes the refusal legible in the transcript when an
agent tries. The real boundary is still an OS-level egress firewall or a
network namespace, and ``AIFORGE_SANDBOX_REQUIRED=1`` already refuses the
kernel outright.

Off with ``AIFORGE_KERNEL_EGRESS=0``.
"""
from __future__ import annotations

import os

_GUARD_SOURCE = '''
"""AIForge kernel egress guard — auto-installed; see runtime/tools/kernel_egress.py."""
import socket as _socket


def _aiforge_install_egress_guard():
    if getattr(_socket, "_aiforge_egress_guarded", False):
        return
    _real_getaddrinfo = _socket.getaddrinfo
    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex
    # Addresses that came out of a resolution the policy allowed. A connect to
    # anything else public is the pre-resolved / hardcoded-IP case.
    _allowed_addrs = set()

    def _policy_refusal(host):
        """The refusal string for a hostname, or None. Fails OPEN on an import
        error: the guard must never make the kernel unusable in an install
        where aiforge_core cannot be imported."""
        try:
            from aiforge_core.net import egress as _eg
        except Exception:
            return None
        try:
            if not host or _eg.is_local_host(str(host)):
                return None
            refusal = _eg.check(str(host) if "://" in str(host)
                                else "https://" + str(host))
            if refusal is None:
                return None
            return "%s — %s" % (refusal.get("error"), refusal.get("hint", ""))
        except Exception:
            return None

    def _is_public(addr):
        try:
            import ipaddress
            ip = ipaddress.ip_address(str(addr).strip("[]"))
        except Exception:
            return False
        return not (ip.is_loopback or ip.is_private or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified)

    def _guarded_getaddrinfo(host, port, *a, **kw):
        why = _policy_refusal(host)
        if why:
            raise OSError(
                "AIForge egress policy refuses %r: %s "
                "(a notebook cell is the same request as web_fetch, through "
                "another transport)" % (host, why))
        res = _real_getaddrinfo(host, port, *a, **kw)
        for entry in res:
            try:
                _allowed_addrs.add(str(entry[4][0]))
            except Exception:
                pass
        return res

    def _check_sockaddr(address):
        try:
            host = address[0] if isinstance(address, (tuple, list)) else None
        except Exception:
            return
        if host is None:
            return                      # AF_UNIX and friends: not egress
        if str(host) in _allowed_addrs or not _is_public(host):
            return
        why = _policy_refusal(host) or "host is not on the egress allowlist"
        raise OSError(
            "AIForge egress policy refuses a connection to %s: %s" % (host, why))

    def _guarded_connect(self, address):
        _check_sockaddr(address)
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        _check_sockaddr(address)
        return _real_connect_ex(self, address)

    _socket.getaddrinfo = _guarded_getaddrinfo
    _socket.socket.connect = _guarded_connect
    _socket.socket.connect_ex = _guarded_connect_ex
    _socket._aiforge_egress_guarded = True


_aiforge_install_egress_guard()
'''


def enabled() -> bool:
    return str(os.environ.get("AIFORGE_KERNEL_EGRESS", "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def guard_source() -> str:
    """Source to exec in the kernel namespace before anything else.

    Empty when the guard is switched off, so the caller stays a one-liner and
    there is no second place deciding whether the guard applies.
    """
    return _GUARD_SOURCE if enabled() else ""


__all__ = ["enabled", "guard_source"]
