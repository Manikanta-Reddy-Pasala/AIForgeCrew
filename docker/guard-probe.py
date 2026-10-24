#!/usr/bin/env python3
"""Inside the guarded box: is the internet really refused? Prints one line.

  NET-BLOCKED          every probe was refused on this machine
  NET-OPEN <how>       a probe got out (open) or its packet left (timeout, …)

The guard REJECTs with a TCP reset, so a guarded probe fails AT ONCE with
"connection refused". Only that — or no route at all — counts as blocked.
A timeout means the packet left this machine (dropped somewhere further on):
the guard did not catch it, so it is NOT counted as blocked. Used by
docker/entrypoint.sh (before the box starts) and run.sh (after `up`).
"""
import errno
import socket

PROBES = (("1.1.1.1", 443), ("2606:4700:4700::1111", 443))
# No route / no address of that family here: the packet cannot leave either.
NO_ROUTE = {errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}


def leak(addr) -> str:
    try:
        socket.create_connection(addr, timeout=3).close()
        return "open"
    except ConnectionRefusedError:
        return ""
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        if exc.errno in NO_ROUTE:
            return ""
        return f"errno-{exc.errno}"


bad = [f"{a[0]}:{how}" for a in PROBES if (how := leak(a))]
print("NET-BLOCKED" if not bad else "NET-OPEN " + ",".join(bad))
