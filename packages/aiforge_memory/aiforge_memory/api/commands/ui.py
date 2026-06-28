"""``aiforge-memory ui`` — serve the read-only web UI.

Lazy-imports the UI server so a CLI invocation that doesn't touch the
``ui`` extra (no FastAPI / starlette installed) doesn't error on
import. The lazy import surfaces a friendly ``ui extra not installed``
message in that case.
"""
from __future__ import annotations

import argparse
import json


def run(args: argparse.Namespace) -> int:
    try:
        from aiforge_memory.ui.server import serve
    except ImportError:
        print(json.dumps({
            "error": "ui extra not installed; "
                     "run: uv pip install '.[ui]'",
        }))
        return 2
    print(json.dumps({
        "serving": f"http://{args.host}:{args.port}",
        "docs":    f"http://{args.host}:{args.port}/docs",
    }))
    serve(host=args.host, port=args.port)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "ui",
        help="Serve the read-only web UI (search, repos, scheduler, memory)",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1; use 0.0.0.0 for LAN)")
    p.add_argument("--port", type=int, default=8767)
    p.set_defaults(func=run)


__all__ = ["run", "register"]
