"""``aiforge-memory ingest-external`` — wire the gap-9 spine to CLI.

Resolves a local file / ``http(s)://`` URL / raw text into one
``Doc_v2`` parent + chunked ``Note_v2`` children via
:func:`aiforge_memory.features.external_ingest.ingest_external_source`.
Concrete connectors (Confluence / Slack / Jira / Notion) layer on top
by calling this same spine with their own pre-fetched text.
"""
from __future__ import annotations

import argparse
import json

from aiforge_memory.features.external_ingest import ingest_external_source

from ._csv import split_csv
from ._driver import driver


def run(args: argparse.Namespace) -> int:
    drv = driver()
    out = ingest_external_source(
        drv,
        source=args.source,
        repo=args.repo,
        source_type=args.source_type,
        title=args.title,
        tags=split_csv(args.tags),
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


def register(sub_parsers) -> None:
    p = sub_parsers.add_parser(
        "ingest-external",
        help="Ingest a local file, http(s) URL, or raw text into AFM "
             "as Doc_v2 + chunked Note_v2 (gap-9).",
    )
    p.add_argument(
        "source",
        help="Path, http(s) URL, or raw text (use - to read from stdin).",
    )
    p.add_argument("--repo", required=True,
                   help="Target AFM Repo.name (must already exist).")
    p.add_argument(
        "--source-type", default="external", dest="source_type",
        help="Free-form label (confluence|slack|jira|notion|manual|"
             "external). Becomes a tag on every node.",
    )
    p.add_argument("--title", help="Human title; defaults to URI.")
    p.add_argument("--tags", help="Comma-separated extra tags.")
    p.set_defaults(func=run)
