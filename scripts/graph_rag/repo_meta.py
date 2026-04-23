#!/usr/bin/env python3
"""Emit one JSON line per repo: build/test/run commands, lang, deploy binding.

Writes to stdout. Consumed by ingest_repo_meta.py.

Usage:
    python repo_meta.py --root ~/Documents/codeRepo > /tmp/repo_meta.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

CFG_DIR = Path(__file__).parent / "config"

EXCLUDE = {
    ".git", ".idea", ".vscode", "node_modules", "dist", "build", "target",
    "SetupRelated", ".aiforge-worktrees", ".claude", ".kubeconfigs",
    "repo-persona", "mongo-migrations", "docker-tests",
}


def load_service_map() -> dict:
    p = CFG_DIR / "service-map.yaml"
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text()) or {}).get("bindings", {}) or {}


def detect_lang(repo: Path) -> str | None:
    if (repo / "pom.xml").exists():
        return "java"
    if (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        return "java"
    if (repo / "package.json").exists():
        try:
            pkg = json.loads((repo / "package.json").read_text())
        except Exception:
            return "node"
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "react" in deps or "next" in deps or "vite" in deps:
            return "react"
        return "node"
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        return "python"
    if (repo / "go.mod").exists():
        return "go"
    return None


def java_meta(repo: Path) -> dict:
    pom = (repo / "pom.xml")
    jdk = 17
    if pom.exists():
        txt = pom.read_text(errors="ignore")
        m = re.search(r"<maven\.compiler\.source>(\d+)</maven\.compiler\.source>", txt)
        if m:
            jdk = int(m.group(1))
    has_wrapper = (repo / "mvnw").exists()
    mvn = "./mvnw" if has_wrapper else "mvn"
    commons = []
    if pom.exists():
        commons = re.findall(
            r"<artifactId>(oneshell-commons-[a-z\-]+)</artifactId>",
            pom.read_text(errors="ignore"),
        )
    return {
        "jdk": jdk,
        "build": {
            "tool": "maven",
            "install": f"{mvn} clean install -DskipTests",
            "compile": f"{mvn} clean compile",
            "test": f"{mvn} test",
            "package": f"{mvn} clean package -DskipTests",
            "run_local": f"{mvn} spring-boot:run",
        },
        "test": {
            "frameworks": ["junit5"],
            "single_class": f"{mvn} test -Dtest=ClassName",
            "single_method": f"{mvn} test -Dtest=ClassName#method",
        },
        "commons_deps": sorted(set(commons)),
    }


def node_meta(repo: Path) -> dict:
    pkg = json.loads((repo / "package.json").read_text())
    mgr = "yarn" if (repo / "yarn.lock").exists() else ("pnpm" if (repo / "pnpm-lock.yaml").exists() else "npm")
    scripts = pkg.get("scripts", {})
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    frameworks = [k for k in ("jest", "vitest", "mocha", "playwright") if k in deps]
    return {
        "build": {
            "tool": mgr,
            "install": f"{mgr} install",
            "dev": scripts.get("dev") or scripts.get("start"),
            "build": scripts.get("build"),
            "lint": scripts.get("lint"),
        },
        "test": {
            "frameworks": frameworks,
            "run": scripts.get("test"),
        },
        "deps": {
            "react": deps.get("react"),
            "vite": deps.get("vite"),
            "next": deps.get("next"),
            "typescript": deps.get("typescript"),
        },
    }


def python_meta(repo: Path) -> dict:
    has_py = (repo / "pyproject.toml").exists()
    has_req = (repo / "requirements.txt").exists()
    install = (
        "pip install -e ." if has_py
        else "pip install -r requirements.txt" if has_req
        else "pip install ."
    )
    entry = None
    for candidate in ("main.py", "app.py", "manage.py", "server.py"):
        if (repo / candidate).exists():
            entry = candidate
            break
    return {
        "build": {
            "tool": "uv" if (repo / "uv.lock").exists() else "pip",
            "install": install,
            "run": f"python {entry}" if entry else None,
        },
        "test": {
            "frameworks": ["pytest"],
            "run": "python -m pytest",
        },
    }


def go_meta(repo: Path) -> dict:
    return {
        "build": {"tool": "go", "install": "go mod download", "build": "go build ./..."},
        "test": {"frameworks": ["go-test"], "run": "go test ./..."},
    }


LANG_META = {
    "java": java_meta,
    "node": node_meta,
    "react": node_meta,
    "python": python_meta,
    "go": go_meta,
}


def build_record(repo: Path, svc_map: dict) -> dict | None:
    lang = detect_lang(repo)
    if not lang:
        return None
    rec = LANG_META[lang](repo)
    rec.update({
        "repo": repo.name,
        "path": str(repo),
        "lang": lang,
    })
    bind = svc_map.get(repo.name, {})
    rec["deploy"] = bind.get("deployments") or {}
    rec["image_prefix"] = bind.get("image_prefix")
    rec["depends_on"] = bind.get("depends_on") or []
    rec["env_required"] = bind.get("env_required") or []
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/Documents/codeRepo"))
    args = ap.parse_args()

    svc_map = load_service_map()
    root = Path(args.root)
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in EXCLUDE:
            continue
        rec = build_record(child, svc_map)
        if rec:
            print(json.dumps(rec), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
