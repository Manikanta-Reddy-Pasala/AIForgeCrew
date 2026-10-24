#!/usr/bin/env python3
"""Inside the guarded box: is the internet really refused? Prints one line.

  NET-BLOCKED          refused on this machine (at least one probe), none got out
  NET-NOROUTE          no route out at all yet (cannot tell; e.g. early at boot)
  NET-OPEN <how>       a probe got out (open) or its packet left (timeout, …)

The guard REJECTs with a TCP reset, so a guarded probe fails AT ONCE with
"connection refused". Only that counts as blocked (a family with no route
at all cannot leave either, but some probe must be refused).
A timeout means the packet left this machine (dropped somewhere further on):
the guard did not catch it, so it is NOT counted as blocked. Used by
docker/entrypoint.sh (before the box starts) and run.sh (after `up`).
"""
import errno
import socket

PROBES = (("1.1.1.1", 443), ("2606:4700:4700::1111", 443))
# No route / no address of that family here: the packet cannot leave either.
NO_ROUTE = {errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}


def result(addr) -> str:
    try:
        socket.create_connection(addr, timeout=3).close()
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        if exc.errno in NO_ROUTE:
            return "noroute"
        return f"errno-{exc.errno}"


got = {a[0]: result(a) for a in PROBES}
bad = [f"{h}:{r}" for h, r in got.items() if r not in ("refused", "noroute")]
if bad:
    print("NET-OPEN " + ",".join(bad))
elif "refused" in got.values():
    print("NET-BLOCKED")
else:
    print("NET-NOROUTE")
