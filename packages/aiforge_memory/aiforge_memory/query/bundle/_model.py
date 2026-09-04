from __future__ import annotations

from dataclasses import dataclass, field


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
    conventions_md: str = ""
    repo_map: str = ""
    # Memory layer
    decisions: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    docs: list[dict] = field(default_factory=list)
    # Cross-repo edges crossing this query's surface
    cross_repo: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    # Semantic domains (repo-level) + flows touching the query's symbols
    domains: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Markdown-ready prompt section. Empty pieces are skipped.

        One method per section. This used to be a single 150-line function
        holding fifteen unrelated formatting rules in one scope, where the only
        thing connecting `lines` at the top to `lines` at the bottom was that
        both were called `lines`.
        """
        pieces = [
            self._intent_md(), self._fastpath_md(), self._services_md(),
            self._domains_md(), self._flows_md(), self._runbook_md(),
            self._conventions_md(), self._repo_map_md(), self._chunks_md(),
            self._files_md(), self._symbols_md(), self._neighbours_md(),
            self._decisions_md(), self._observations_md(), self._notes_md(),
            self._docs_md(), self._cross_repo_md(), self._sources_md(),
            self._errors_md(),
        ]
        return "\n\n".join(p for p in pieces if p)

    # ── sections ──────────────────────────────────────────────────────────
    # Each returns "" when it has nothing to say. A section whose SOURCE list
    # is non-empty but whose entries all render blank still emits its heading —
    # that was the behaviour before and it is how a reader spots an empty
    # source rather than a missing one.

    @staticmethod
    def _block(title: str, lines: list[str]) -> str:
        return "\n".join([f"## {title}", *lines])

    def _intent_md(self) -> str:
        return f"## Intent\n- {self.intent}" if self.intent else ""

    def _fastpath_md(self) -> str:
        return f"## Fastpath\n- {self.fastpath_hit}" if self.fastpath_hit else ""

    def _services_md(self) -> str:
        if not self.services:
            return ""
        return self._block("Services", [
            f"- **{svc['name']}** ({svc.get('role','?')})"
            f" — {svc.get('description','')}"
            for svc in self.services[:3]
        ])

    def _domains_md(self) -> str:
        if not self.domains:
            return ""
        lines = []
        for d in self.domains[:5]:
            desc = (d.get("description") or "").strip()
            svcs = ", ".join(d.get("services", [])[:6])
            tail = (f" — {desc}" if desc else "") + (f" [{svcs}]" if svcs else "")
            lines.append(f"- **{d['name']}**{tail}")
        return self._block("Domains", lines)

    def _flows_md(self) -> str:
        if not self.flows:
            return ""
        return self._block("Flows", [
            f"- **{f['name']}**: " + " → ".join(f.get("steps", [])[:8])
            for f in self.flows[:5]
        ])

    def _runbook_md(self) -> str:
        if not self.runbook_md:
            return ""
        return "## Runbook (top of repo)\n" + self.runbook_md[:2000]

    def _conventions_md(self) -> str:
        if not self.conventions_md:
            return ""
        return "## Conventions (.cursorrules)\n" + self.conventions_md[:2000]

    def _repo_map_md(self) -> str:
        if not self.repo_map:
            return ""
        return "## Repo Map\n```\n" + self.repo_map + "\n```"

    def _chunks_md(self) -> str:
        if not self.chunks:
            return ""
        lines = []
        for c in self.chunks[:5]:
            path = c.get("file_path", "")
            text = (c.get("text") or "").strip()
            if path and text:
                lines.append(f"### `{path}`\n```\n{text}\n```")
        return self._block("Relevant Code Chunks", lines)

    def _files_md(self) -> str:
        if not self.files:
            return ""
        lines = []
        for f in self.files[:8]:
            summary = (f.get("summary") or "").strip()
            lines.append(f"- `{f['path']}` — {summary}" if summary
                         else f"- `{f['path']}`")
        return self._block("Anchor files", lines)

    @staticmethod
    def _params_str(params_json: str) -> str:
        """Compact `params: name:type, …` line — agent-friendly stub generation.

        Silent on anything unparseable: the field is model output, and a
        malformed one must not cost the whole symbol its line.
        """
        import json as _json
        if not params_json:
            return ""
        try:
            ps = _json.loads(params_json)
        except (ValueError, TypeError):
            return ""
        if not isinstance(ps, list) or not ps:
            return ""
        return " params: " + ", ".join(
            f"{p.get('name','?')}:{p.get('type','?')}" for p in ps[:8]
        )

    def _symbols_md(self) -> str:
        if not self.symbols:
            return ""
        lines = []
        for s in self.symbols[:12]:
            sig = s.get("signature", "")
            describe = ((s.get("summary") or "").strip()
                        or (s.get("doc") or "").strip())
            tag = " ⚠ DEPRECATED" if s.get("deprecated") else ""
            params_str = self._params_str(s.get("params_json") or "")
            head = f"- `{s['fqname']}`{tag} — `{sig}`"
            lines.append(f"{head} — {describe}{params_str}" if describe
                         else f"{head}{params_str}")
        return self._block("Symbols", lines)

    def _neighbours_md(self) -> str:
        if not (self.callers or self.callees):
            return ""
        lines = [f"- caller of {c['target']}: `{c['fqname']}`"
                 for c in (self.callers or [])[:6]]
        lines += [f"- callee of {c['source']}: `{c['fqname']}`"
                  for c in (self.callees or [])[:6]]
        return self._block("Call neighbours", lines)

    def _decisions_md(self) -> str:
        if not self.decisions:
            return ""
        lines = []
        for d in self.decisions[:5]:
            rationale = (d.get("rationale") or "").strip()
            head = f"- **{d.get('title', '')}** ({d.get('status') or 'active'})"
            if rationale:
                head += f" — {rationale[:200]}"
            lines.append(head)
        return self._block("Decisions", lines)

    def _observations_md(self) -> str:
        if not self.observations:
            return ""
        return self._block("Observations", [
            f"- *{o.get('kind') or 'note'}* — {(o.get('text') or '').strip()[:240]}"
            for o in self.observations[:5]
        ])

    def _notes_md(self) -> str:
        if not self.notes:
            return ""
        return self._block("Notes", [
            f"- **{n.get('title') or 'Note'}** — "
            f"{(n.get('body') or '').strip()[:240]}"
            for n in self.notes[:5]
        ])

    def _docs_md(self) -> str:
        if not self.docs:
            return ""
        return self._block("External Docs", [
            f"- **{d.get('title') or 'Doc'}** ({d.get('url') or ''}) — "
            f"{(d.get('body') or '').strip()[:240]}"
            for d in self.docs[:5]
        ])

    def _cross_repo_md(self) -> str:
        if not self.cross_repo:
            return ""
        lines = []
        for e in self.cross_repo[:5]:
            ev = ", ".join(e.get("evidence", [])[:3])
            lines.append(
                f"- `{e['src']}` → `{e['dst']}` via {e['via']} "
                f"(conf {e.get('confidence',0):.2f}; {ev})"
            )
        return self._block("Related repos", lines)

    def _sources_md(self) -> str:
        if not self.sources_used:
            return ""
        return "_sources: " + ", ".join(self.sources_used) + "_"

    def _errors_md(self) -> str:
        if not self.errors:
            return ""
        return "_errors: " + "; ".join(self.errors) + "_"
