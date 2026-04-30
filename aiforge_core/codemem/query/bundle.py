"""ContextBundle builder — translator → Cypher → token-budget pack.

Public surface:
    bundle.query(text, *, repo, driver, role='doer', token_budget=4000)
        -> ContextBundle
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from aiforge_core.codemem.query import fastpath, translator


@dataclass
class ContextBundle:
    repo: str = ""
    intent: str = ""
    fastpath_hit: str = ""           # kind:value if matched
    services: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)
    callers: list[dict] = field(default_factory=list)
    callees: list[dict] = field(default_factory=list)
    runbook_md: str = ""
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Markdown-ready prompt section. Empty pieces are skipped."""
        out: list[str] = []

        if self.intent:
            out.append(f"## Intent\n- {self.intent}")
        if self.fastpath_hit:
            out.append(f"## Fastpath\n- {self.fastpath_hit}")

        if self.services:
            lines = ["## Services"]
            for svc in self.services[:3]:
                lines.append(
                    f"- **{svc['name']}** ({svc.get('role','?')})"
                    f" — {svc.get('description','')}"
                )
            out.append("\n".join(lines))

        if self.runbook_md:
            out.append("## Runbook (top of repo)\n" + self.runbook_md[:2000])

        if self.files:
            lines = ["## Anchor files"]
            for f in self.files[:8]:
                summary = (f.get("summary") or "").strip()
                if summary:
                    lines.append(f"- `{f['path']}` — {summary}")
                else:
                    lines.append(f"- `{f['path']}`")
            out.append("\n".join(lines))

        if self.symbols:
            lines = ["## Symbols"]
            for s in self.symbols[:12]:
                sig = s.get("signature", "")
                lines.append(f"- `{s['fqname']}` — `{sig}`")
            out.append("\n".join(lines))

        if self.callers or self.callees:
            lines = ["## Call neighbours"]
            for c in (self.callers or [])[:6]:
                lines.append(f"- caller of {c['target']}: `{c['fqname']}`")
            for c in (self.callees or [])[:6]:
                lines.append(f"- callee of {c['source']}: `{c['fqname']}`")
            out.append("\n".join(lines))

        if self.sources_used:
            out.append("_sources: " + ", ".join(self.sources_used) + "_")
        if self.errors:
            out.append("_errors: " + "; ".join(self.errors) + "_")

        return "\n\n".join(out)


def query(
    text: str,
    *,
    repo: str,
    driver,
    role: str = "doer",
    token_budget: int = 4000,
) -> ContextBundle:
    bundle = ContextBundle(repo=repo)

    # Fastpath
    hit = fastpath.detect(text)
    if hit:
        bundle.fastpath_hit = f"{hit.kind}:{hit.value}"

    # Translator (always run — fastpath is auxiliary)
    g = translator.translate(text, repo=repo, driver=driver)
    bundle.intent = g.intent
    bundle.errors.extend(g.errors)
    if g.errors:
        bundle.sources_used.append("translator(partial)")
    else:
        bundle.sources_used.append("translator")

    # Hydrate Service rows
    if g.services:
        bundle.services = _services_rows(driver, repo=repo, names=g.services)
        bundle.sources_used.append("services")

    # Hydrate File rows (with summary)
    file_paths = list(g.files)
    if hit and hit.kind == "file":
        file_paths = [hit.value] + file_paths
    if file_paths:
        bundle.files = _files_rows(driver, repo=repo, paths=file_paths)
        bundle.sources_used.append("files")

    # Hydrate Symbol rows
    sym_fqnames = list(g.symbols)
    if hit and hit.kind == "symbol":
        # fastpath symbol guess — search by terminal name
        bundle.symbols = _symbols_by_terminal_name(
            driver, repo=repo, name=hit.value.rsplit(".", 1)[-1],
        )
    if sym_fqnames:
        bundle.symbols = _symbols_rows(driver, repo=repo, fqnames=sym_fqnames) \
            + bundle.symbols
        bundle.sources_used.append("symbols")

    # Call neighbours (1 hop) for top symbol
    if bundle.symbols:
        primary = bundle.symbols[0]["fqname"]
        bundle.callers, bundle.callees = _call_neighbours(
            driver, repo=repo, fqname=primary, hops=g.hops,
        )
        bundle.sources_used.append("calls")

    # Repo runbook (always cheap to fetch)
    bundle.runbook_md = _runbook_for(driver, repo=repo)
    if bundle.runbook_md:
        bundle.sources_used.append("runbook")

    # Token budget — drop low-priority sections if over (rough; chars≈4tok)
    rendered = bundle.render()
    char_budget = token_budget * 4
    if len(rendered) > char_budget:
        # drop callers/callees first
        bundle.callers = []
        bundle.callees = []
        if len(bundle.render()) > char_budget:
            bundle.symbols = bundle.symbols[:6]
            bundle.files = bundle.files[:4]

    return bundle


def _services_rows(driver, *, repo: str, names: list[str]) -> list[dict]:
    cy = (
        "MATCH (s:Service {repo:$repo}) WHERE s.name IN $names "
        "RETURN s.name AS name, s.role AS role, s.description AS description, "
        "       s.port AS port, s.tech_stack AS tech_stack, s.source AS source"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, names=names)]


def _files_rows(driver, *, repo: str, paths: list[str]) -> list[dict]:
    cy = (
        "MATCH (f:File_v2 {repo:$repo}) WHERE f.path IN $paths "
        "RETURN f.path AS path, f.lang AS lang, f.lines AS lines, "
        "       coalesce(f.summary,'') AS summary, "
        "       coalesce(f.purpose_tags,[]) AS purpose_tags"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, paths=paths)]


def _symbols_rows(driver, *, repo: str, fqnames: list[str]) -> list[dict]:
    cy = (
        "MATCH (s:Symbol_v2 {repo:$repo}) WHERE s.fqname IN $fqnames "
        "RETURN s.fqname AS fqname, s.kind AS kind, "
        "       s.file_path AS file_path, s.signature AS signature"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, fqnames=fqnames)]


def _symbols_by_terminal_name(driver, *, repo: str, name: str) -> list[dict]:
    cy = (
        "MATCH (s:Symbol_v2 {repo:$repo}) "
        "WHERE s.fqname ENDS WITH $suffix "
        "RETURN s.fqname AS fqname, s.kind AS kind, "
        "       s.file_path AS file_path, s.signature AS signature LIMIT 6"
    )
    with driver.session() as sess:
        return [dict(r) for r in sess.run(cy, repo=repo, suffix=f"::{name}")]


def _call_neighbours(
    driver, *, repo: str, fqname: str, hops: int = 1,
) -> tuple[list[dict], list[dict]]:
    callers_cy = (
        "MATCH (caller:Symbol_v2 {repo:$repo})-[:CALLS]->(t:Symbol_v2 {fqname:$fq}) "
        "RETURN caller.fqname AS fqname LIMIT 8"
    )
    callees_cy = (
        "MATCH (s:Symbol_v2 {repo:$repo, fqname:$fq})-[:CALLS]->(callee:Symbol_v2) "
        "RETURN callee.fqname AS fqname LIMIT 8"
    )
    with driver.session() as sess:
        callers = [
            {"fqname": r["fqname"], "target": fqname}
            for r in sess.run(callers_cy, repo=repo, fq=fqname)
        ]
        callees = [
            {"fqname": r["fqname"], "source": fqname}
            for r in sess.run(callees_cy, repo=repo, fq=fqname)
        ]
    return callers, callees


def _runbook_for(driver, *, repo: str) -> str:
    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN coalesce(r.runbook_md,'') AS rb",
            n=repo,
        ).single()
    return row["rb"] if row else ""
