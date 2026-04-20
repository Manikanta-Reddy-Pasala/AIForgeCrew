#!/usr/bin/env python3
"""Multi-repo RAG reindex.

Runs on Mac Studio (or wherever AIForgeCrew + target repos are checked out).
Indexes local AIForgeCrew docs PLUS external repo code.

Usage:
  /Users/manikanta/AIForgeCrew/.venv/bin/python scripts/rag-reindex-multi.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aiforge_core.rag import RagIndex

CODE_REPO_ROOT = Path.home() / "codeRepo"

EXTERNAL = [
    # PosPythonBackend — primary target (bank OCR, invoice OCR, AI assistant)
    (
        "posbackend",
        CODE_REPO_ROOT / "PosPythonBackend",
        [
            "app/**/*.py",
            "tests/**/*.py",
            "main.py",
            "*.md",
            "requirements.txt",
        ],
    ),
    # TallyConnector — Java sync
    (
        "tally",
        CODE_REPO_ROOT / "TallyConnector",
        [
            "src/main/java/**/*.java",
            "src/main/resources/**/*.xml",
            "*.md",
            "pom.xml",
        ],
    ),
    # MongoDbService — Java gateway
    (
        "mongodbsvc",
        CODE_REPO_ROOT / "MongoDbService",
        [
            "src/main/java/**/*.java",
            "*.md",
            "pom.xml",
        ],
    ),
    # PosDataSyncService — sync service
    (
        "pds",
        CODE_REPO_ROOT / "PosDataSyncService",
        [
            "src/main/java/**/*.java",
            "*.md",
            "pom.xml",
        ],
    ),
]


def main():
    idx = RagIndex(REPO_ROOT)
    print(f"Indexing AIForgeCrew at {REPO_ROOT}")
    for label, root, _ in EXTERNAL:
        if root.exists():
            print(f"  + {label}: {root}")
        else:
            print(f"  - {label}: {root} (MISSING — skipping)")

    # Keep only existing externals
    externals = [(l, r, g) for l, r, g in EXTERNAL if r.exists()]
    stats = idx.reindex(external_repos=externals)
    print(f"\nIndexed: {stats}")


if __name__ == "__main__":
    main()
