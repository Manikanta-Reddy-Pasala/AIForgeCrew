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

        if self.domains:
            lines = ["## Domains"]
            for d in self.domains[:5]:
                desc = (d.get("description") or "").strip()
                svcs = ", ".join(d.get("services", [])[:6])
                tail = (f" — {desc}" if desc else "") + (f" [{svcs}]" if svcs else "")
                lines.append(f"- **{d['name']}**{tail}")
            out.append("\n".join(lines))

        if self.flows:
            lines = ["## Flows"]
            for f in self.flows[:5]:
                steps = " → ".join(f.get("steps", [])[:8])
                lines.append(f"- **{f['name']}**: {steps}")
            out.append("\n".join(lines))

        if self.runbook_md:
            out.append("## Runbook (top of repo)\n" + self.runbook_md[:2000])

        if self.conventions_md:
            out.append("## Conventions (.cursorrules)\n" + self.conventions_md[:2000])

        if self.repo_map:
            out.append("## Repo Map\n```\n" + self.repo_map + "\n```")

        if self.chunks:
            lines = ["## Relevant Code Chunks"]
            for c in self.chunks[:5]:
                # Compact output
                path = c.get("file_path", "")
                text = (c.get("text") or "").strip()
                if path and text:
                    lines.append(f"### `{path}`\n```\n{text}\n```")
            out.append("\n".join(lines))

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
            import json as _json
            lines = ["## Symbols"]
            for s in self.symbols[:12]:
                sig = s.get("signature", "")
                summ = (s.get("summary") or "").strip()
                fallback = (s.get("doc") or "").strip()
                describe = summ or fallback
                tag = " ⚠ DEPRECATED" if s.get("deprecated") else ""
                # Compact params line — agent-friendly stub generation.
                params_str = ""
                pj = s.get("params_json") or ""
                if pj:
                    try:
                        ps = _json.loads(pj)
                        if isinstance(ps, list) and ps:
                            params_str = " params: " + ", ".join(
                                f"{p.get('name','?')}:{p.get('type','?')}"
                                for p in ps[:8]
                            )
                    except (ValueError, TypeError):
                        pass
                if describe:
                    lines.append(
                        f"- `{s['fqname']}`{tag} — `{sig}` — {describe}{params_str}"
                    )
                else:
                    lines.append(f"- `{s['fqname']}`{tag} — `{sig}`{params_str}")
            out.append("\n".join(lines))

        if self.callers or self.callees:
            lines = ["## Call neighbours"]
            for c in (self.callers or [])[:6]:
                lines.append(f"- caller of {c['target']}: `{c['fqname']}`")
            for c in (self.callees or [])[:6]:
                lines.append(f"- callee of {c['source']}: `{c['fqname']}`")
            out.append("\n".join(lines))

        if self.decisions:
            lines = ["## Decisions"]
            for d in self.decisions[:5]:
                title = d.get("title", "")
                rationale = (d.get("rationale") or "").strip()
                status = d.get("status") or "active"
                head = f"- **{title}** ({status})"
                if rationale:
                    head += f" — {rationale[:200]}"
                lines.append(head)
            out.append("\n".join(lines))

        if self.observations:
            lines = ["## Observations"]
            for o in self.observations[:5]:
                kind = o.get("kind") or "note"
                text = (o.get("text") or "").strip()
                lines.append(f"- *{kind}* — {text[:240]}")
            out.append("\n".join(lines))

        if self.notes:
            lines = ["## Notes"]
            for n in self.notes[:5]:
                title = n.get("title") or "Note"
                body = (n.get("body") or "").strip()
                lines.append(f"- **{title}** — {body[:240]}")
            out.append("\n".join(lines))

        if self.docs:
            lines = ["## External Docs"]
            for d in self.docs[:5]:
                title = d.get("title") or "Doc"
                url = d.get("url") or ""
                body = (d.get("body") or "").strip()
                lines.append(f"- **{title}** ({url}) — {body[:240]}")
            out.append("\n".join(lines))

        if self.cross_repo:
            lines = ["## Related repos"]
            for e in self.cross_repo[:5]:
                ev = ", ".join(e.get("evidence", [])[:3])
                lines.append(
                    f"- `{e['src']}` → `{e['dst']}` via {e['via']} "
                    f"(conf {e.get('confidence',0):.2f}; {ev})"
                )
            out.append("\n".join(lines))

        if self.sources_used:
            out.append("_sources: " + ", ".join(self.sources_used) + "_")
        if self.errors:
            out.append("_errors: " + "; ".join(self.errors) + "_")

        return "\n\n".join(out)
