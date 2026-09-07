"""Build docs/AIForgeCrew-CTO-Summary.pptx — a THREE-page executive summary.

Audience: a CTO, not an engineer. Three pages, one question each, and every
page is deliberately dense: an earlier fourteen-page cut said the same things
and nobody reads fourteen pages in a room.

  1. What is it, and how does the work get done? — the pipeline and the three
     chat modes; the library of rules, skills and workflows the agent writes
     for itself and a nightly sweep de-duplicates; memory as markdown plus one
     SQLite and NO database anywhere; integrations, the real toolchain, deploy.
  2. Containment, the cost ceiling, and the limits we state out loud — one
     egress policy under three transports, access control and the untrusted-
     input problem, where the source actually goes, rate ceiling and request
     meter, secrets at rest, supply chain, and what we do NOT claim to hold.
  3. The evidence and the asks — the SonarQube numbers and why the zero is
     real, what that number does NOT say (agent output, sizing, restore drill
     and audit evidence are all unmeasured), the comparison against the usual
     stack, and the five decisions we need.

Run it:

    uv run --with python-pptx python docs/build_cto_summary_deck.py

Primitives are imported from build_overview_deck so the two decks stay one
visual system: change a colour there and both follow.

TWO RULES THIS FILE ENFORCES IN CODE, because both have burned a deck before:

* geometry goes through the sibling deck's ``_e`` helper — float EMU writes an
  invalid pptx that saves cleanly and PowerPoint then refuses;
* card bodies are NEVER hand-wrapped. ``card_row`` measures the text and sizes
  the box, so a sentence that grows cannot quietly spill past its border on a
  machine with no renderer to notice. At three pages the cards carry far more
  text than before, which makes that measurement the thing holding the deck
  together — verify a rebuild before sending it.

Numbers are MEASURED on main and dated on the slide. Do not quote a control
that has since been reverted — the Sonar security *gate* was (c6832c4d), so it
is deliberately absent here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_overview_deck import (  # noqa: E402
    AMBER,
    AMBER_T,
    BLUE,
    BLUE_T,
    GREEN,
    GREEN_T,
    INK,
    LINE,
    MUTED,
    PLUM,
    PLUM_T,
    RED,
    RED_T,
    SLATE,
    SLATE_T,
    WHITE,
    H,
    W,
    _e,
    arrow,
    box,
    chip,
    slide_base,
    text,
)
from pptx import Presentation  # noqa: E402
from pptx.enum.shapes import MSO_SHAPE  # noqa: E402
from pptx.enum.text import PP_ALIGN  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402

OUT = Path(__file__).resolve().parent / "AIForgeCrew-CTO-Summary.pptx"

# ---------------------------------------------------------------- facts ---
# READ OFF THE SCANNER, not off a green pipeline. Every figure below comes from
# one API call against the analysis named in COMMIT:
#
#   /api/measures/component?component=aiforgecrew&metricKeys=...
#   /api/issues/search?resolved=false   ·   /api/hotspots/search?status=TO_REVIEW
#
# Re-measure before reusing this deck, and change them HERE — the slides read
# these names, so a stale number cannot survive in one corner of a chart.
COMMIT = "7d9e60ae"
MEASURED = "2026-09-06"
M = {
    "bugs": "0",
    "vulnerabilities": "0",
    "code_smells": "0",
    "hotspots": "0",
    "open_issues": "0",
    "ratings": "A\u00b7A\u00b7A",
    "tests": "8,606",
    "failures": "3",          # the 3 live-model tests; see the quality slide
    "coverage": "82.8%",      # Sonar-scoped
    "coverage_raw": "86.7%",  # pytest-cov line coverage on the same run
    "ncloc": "71,204",
    "files": "542",
    "duplication": "0.1%",
    # Product surface, measured the same way: len(CATALOG) in the chat tool
    # schemas, the role list in agents.yaml, and prefix counts over CATALOG.
    "chat_tools": "108",
    "roles": "19",
    "jira_tools": "21",
    "confluence_tools": "14",
    "gitlab_tools": "10",
    "codegraph_tools": "5",
}

AS_OF = f"Measured on main @ {COMMIT}, {MEASURED}"
L, WD = Inches(0.62), Inches(12.1)

# Segoe UI averages ~0.47 em per character over ordinary prose. Rounded DOWN to
# 0.50 so the estimate errs towards a taller box: a card with slack looks fine,
# a card one line short of its text does not.
_EM = 0.50
_LEAD = 1.30          # line height as a multiple of the point size
_PAD = Inches(0.18)   # inner padding, left and right, top and bottom
_GAP = Inches(0.20)   # gutter between cards in a row
_ZERO = Inches(0.0)


def _wrapped_lines(s: str, width_in: float, size: float) -> int:
    """Lines a string takes in a box of `width_in` inches at `size` points."""
    per_line = max(1, int(width_in / (size * _EM / 72.0)))
    total = 0
    for para in s.split("\n"):
        words, line = para.split(), ""
        n = 1
        for wd in words:
            trial = f"{line} {wd}".strip()
            if len(trial) <= per_line:
                line = trial
            else:
                n += 1
                line = wd
        total += n
    return total


def _text_height(s: str, width_in: float, size: float) -> float:
    return _wrapped_lines(s, width_in, size) * size * _LEAD / 72.0


def metric_strip(s, y, items):
    """The headline row: big number over a short label, hairline separators."""
    cw = WD / len(items)
    for i, (big, lab, col) in enumerate(items):
        x = L + i * cw
        text(s, x, y, cw, Inches(0.42), big, size=25, bold=True, color=col,
             align=PP_ALIGN.CENTER)
        text(s, _e(x + Inches(0.08)), _e(y + Inches(0.44)),
             _e(cw - Inches(0.16)), Inches(0.42), lab, size=10.5, color=MUTED,
             align=PP_ALIGN.CENTER)
        if i:
            sep = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, _e(x),
                                     _e(y + Inches(0.04)), Pt(0.75),
                                     Inches(0.62))
            sep.fill.solid()
            sep.fill.fore_color.rgb = LINE
            sep.line.fill.background()
            sep.shadow.inherit = False


def card_row(s, y, cards, *, x0=None, total_w=None, gap=_GAP,
             head_size=12.5, body_size=10.5, min_h=_ZERO):
    """A row of equal-width, equal-height bullet cards.

    Height is MEASURED from the wrapped text, never guessed, and the row is as
    tall as its longest card. Each card is (head, [bullets], fill, edge);
    bullets are whole sentences — the box wraps them, this function does not.
    """
    x0 = L if x0 is None else x0
    total_w = WD if total_w is None else total_w
    n = len(cards)
    cw = (total_w - gap * (n - 1)) / n
    inner = (cw - _PAD * 2) / Inches(1)

    height = min_h
    for head, bullets, _f, _e2 in cards:
        h = _PAD * 2
        if head:
            h += Inches(_text_height(head, inner, head_size)) + Inches(0.10)
        for b in bullets:
            h += Inches(_text_height(f"·  {b}", inner, body_size))
            h += Inches(0.05)
        height = max(height, h)

    for i, (head, bullets, fill, edge) in enumerate(cards):
        x = x0 + i * (cw + gap)
        sp = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, _e(x), _e(y),
                                _e(cw), _e(height))
        sp.fill.solid()
        sp.fill.fore_color.rgb = fill
        sp.line.color.rgb = edge
        sp.line.width = Pt(1.0)
        sp.shadow.inherit = False
        cy = y + _PAD
        if head:
            hh = Inches(_text_height(head, inner, head_size))
            text(s, _e(x + _PAD), _e(cy), _e(cw - _PAD * 2), _e(hh), head,
                 size=head_size, bold=True, color=edge)
            cy += hh + Inches(0.10)
        for b in bullets:
            bh = Inches(_text_height(f"·  {b}", inner, body_size))
            text(s, _e(x + _PAD), _e(cy), _e(cw - _PAD * 2), _e(bh),
                 f"·  {b}", size=body_size)
            cy += bh + Inches(0.05)
    return height


def notes(s, body: str) -> None:
    """Park the prose in the speaker notes.

    The slides went visual because fourteen pages of cards were unreadable in
    a room — but a CTO deck still has to answer the hard question when it is
    asked out loud. Every sentence the boxes no longer carry lives here, so
    the presenter has it and the slide does not.
    """
    s.notes_slide.notes_text_frame.text = body.strip()


def chips(s, x, y, w, items, *, fill=WHITE, edge=LINE, color=INK, size=9,
          gap=Inches(0.08)):
    """A vertical stack of pills. Returns the y below the last one."""
    for it in items:
        chip(s, x, y, w, it, fill=fill, edge=edge, color=color, size=size)
        y = _e(y + Inches(0.30) + gap)
    return y


# ------------------------------------------------------------- page 1 ------
def how_it_works(prs):
    s = slide_base(
        prs, "AIForgeCrew — a coding agent platform you run yourself",
        "A ticket becomes a merge request. On your hardware, against your "
        "model, with no database anywhere in it.",
        f"{AS_OF}. One Python service, one port, one folder of state.")

    metric_strip(s, Inches(1.36), [
        (M["chat_tools"], "tools", BLUE),
        (M["roles"], "agent roles", BLUE),
        ("3", "chat modes", BLUE),
        ("0", "databases", GREEN),
        (M["tests"], "tests", GREEN),
        ("0", "Sonar findings", GREEN),
    ])

    # ── row 1: in → process → model ────────────────────────────────────────
    y = Inches(2.30)
    for i, (lab, sub, f, e) in enumerate([
            ("Ticket", "Jira · GitLab", BLUE_T, BLUE),
            ("Chat", "ask · plan · team", GREEN_T, GREEN),
            ("Schedule", "cron, unattended", PLUM_T, PLUM)]):
        box(s, L, _e(y + i * Inches(0.56)), Inches(2.10), Inches(0.48),
            lab, sub, fill=f, edge=e, size=11, sub_size=8)
        arrow(s, Inches(2.72), _e(y + i * Inches(0.56) + Inches(0.24)),
              Inches(3.30), Inches(2.86))

    box(s, Inches(3.30), y, Inches(3.30), Inches(1.60),
        "One process · :8799",
        "chat engine · pipeline\nmemory · integrations\nscheduler",
        fill=SLATE_T, edge=SLATE, size=12, sub_size=9)
    arrow(s, Inches(6.60), Inches(3.10), Inches(7.20), Inches(3.10))
    box(s, Inches(7.20), y, Inches(2.60), Inches(1.60), "Your endpoint",
        "LM Studio · vLLM · mlx\non hardware you own\n\ncloud only if YOU\nregister one",
        fill=GREEN_T, edge=GREEN, size=12, sub_size=8.5)

    box(s, Inches(10.10), y, Inches(2.62), Inches(1.60), "No database",
        "no vector store\nno Postgres · no Neo4j\nno broker · no GPU\n\n"
        "markdown + one SQLite",
        fill=WHITE, edge=BLUE, size=12, sub_size=8.5, label_color=BLUE)

    # ── row 2: the pipeline ───────────────────────────────────────────────
    y2 = Inches(4.10)
    text(s, L, _e(y2 - Inches(0.26)), Inches(4.0), Inches(0.22),
         "How a ticket becomes a merge request", size=10, bold=True,
         color=MUTED)
    stages = ["Triage", "Enhancer", "Architect", "Planner", "Verifier"]
    bw = Inches(1.44)
    for i, st in enumerate(stages):
        x = L + i * (bw + Inches(0.13))
        box(s, x, y2, bw, Inches(0.42), st, "", fill=BLUE_T, edge=BLUE,
            size=10)
        if i < 4:
            arrow(s, _e(x + bw), _e(y2 + Inches(0.21)),
                  _e(x + bw + Inches(0.13)), _e(y2 + Inches(0.21)))
    arrow(s, _e(L + 5 * (bw + Inches(0.13)) - Inches(0.13)),
          _e(y2 + Inches(0.21)), Inches(8.52), _e(y2 + Inches(0.21)))
    box(s, Inches(8.52), y2, Inches(2.10), Inches(0.42), "Build loop", "",
        fill=GREEN_T, edge=GREEN, size=10)
    arrow(s, Inches(10.62), _e(y2 + Inches(0.21)), Inches(10.75),
          _e(y2 + Inches(0.21)))
    box(s, Inches(10.75), y2, Inches(1.97), Inches(0.42), "Merge request", "",
        fill=SLATE_T, edge=SLATE, size=10)
    text(s, Inches(8.52), _e(y2 + Inches(0.46)), Inches(4.20), Inches(0.36),
         "4 parallel subtasks, each in its own git worktree  ·  "
         "Validator → Live-verifier → Learner", size=8.5, color=MUTED)

    # ── row 3: the two loops that make it compound ────────────────────────
    y3 = Inches(5.16)
    box(s, L, y3, Inches(6.00), Inches(1.30), "It writes its own procedures",
        "", fill=AMBER_T, edge=AMBER, size=11.5)
    for i, lab in enumerate(["Rules", "Skills", "Workflows"]):
        chip(s, _e(L + Inches(0.22) + i * Inches(1.30)), _e(y3 + Inches(0.42)),
             Inches(1.18), lab, fill=WHITE, edge=AMBER, color=AMBER)
    chip(s, Inches(4.62), _e(y3 + Inches(0.42)), Inches(1.22),
         "nightly merge", fill=WHITE, edge=AMBER, color=AMBER)
    arrow(s, Inches(4.42), _e(y3 + Inches(0.57)), Inches(4.62),
          _e(y3 + Inches(0.57)), color=AMBER)
    text(s, _e(L + Inches(0.22)), _e(y3 + Inches(0.86)), Inches(5.6),
         Inches(0.34),
         "markdown the agent writes and reuses — a fix found once becomes a "
         "procedure, and duplicates are swept up nightly", size=8.5,
         color=MUTED)

    box(s, Inches(6.90), y3, Inches(5.82), Inches(1.30),
        "It remembers, without a database", "", fill=GREEN_T, edge=GREEN,
        size=11.5)
    hops = [("Desktop", 1.22), ("Redact", 1.10), ("Team admin", 1.30),
            ("Company", 1.20)]
    x = Inches(7.12)
    for i, (lab, w) in enumerate(hops):
        chip(s, x, _e(y3 + Inches(0.42)), Inches(w), lab, fill=WHITE,
             edge=GREEN, color=GREEN)
        x = _e(x + Inches(w))
        if i < len(hops) - 1:
            arrow(s, x, _e(y3 + Inches(0.57)), _e(x + Inches(0.22)),
                  _e(y3 + Inches(0.57)), color=GREEN)
            x = _e(x + Inches(0.22))
    text(s, Inches(7.12), _e(y3 + Inches(0.86)), Inches(5.40), Inches(0.34),
         "only distilled notes travel · credentials blocked before they are "
         "offered · delete the folder and it is gone", size=8.5, color=MUTED)

    notes(s, """
One FastAPI process, port 8799, React UI plus REST and streaming; chat engine,
agent pipeline, memory, integrations and job scheduler are libraries inside it.
Python 3.12 and one model endpoint; no GPU, torch, Postgres, Neo4j, vector
store or message broker. Runs on a desktop, a NUC, a server under systemd or a
container — the same single command, and an unset admin URL means this box is
it. Upgrade is git pull and run.sh, which converges the environment and
migrates in place.

Pipeline: Triage, Enhancer, Architect, Planner, Verifier, then a build loop of
up to four parallel subtasks each in its own git worktree (Doer edits, tests
run, Feedback reads failures, Refiner fixes), then Validator gates,
Live-verifier runs the real recipe, Learner writes memory back, and the result
is a merge request a human reviews. Reasoning is plain text, so any model
drives it — no vendor function-calling. Simple mode writes, Plan mode is
read-only, Team mode runs the pipeline.

Library: rules, skills and workflows are markdown the agent writes and reuses;
a nightly sweep merges near-duplicates so the prompt does not bloat, and a
merge unions the members' scopes rather than silently narrowing one.

Memory: each desktop compacts locally, a client-side redaction filter blocks
credential-shaped and private notes before anything is advertised, one team
admin merges, and one admin can serve many independent fleets. Only distilled
knowledge nodes travel; transcripts, captures and working notes never leave the
machine that made them. Every merge is snapshotted with a revert endpoint.

Integrations: Jira 21 tools, Confluence 14, GitLab 10, plus GitHub, email and
any MCP server; a tool with no credentials hides itself. A tree-sitter and
PageRank repo map plus 5 knowledge-graph tools choose context instead of
pasting it. A local vision model can screenshot the running app. The real
toolchain is present: shell, language server, type-checker, test runner, an
IPython kernel.
""")


# ------------------------------------------------------------- page 2 ------
def containment_visual(prs):
    s = slide_base(
        prs, "Containment — one policy, every path",
        "A refused fetch was rerouted through a shell curl, then a notebook "
        "cell, and it worked. So the policy sits under all three.",
        f"{AS_OF}. A guard, not a sandbox: an OS egress firewall is still the "
        "outer boundary, and that limit is printed in the product's own docs.")

    metric_strip(s, Inches(1.36), [
        (M["vulnerabilities"], "vulnerabilities", GREEN),
        (M["hotspots"], "hotspots", GREEN),
        ("0", "telemetry", GREEN),
        ("0", "CERT_NONE in the tree", GREEN),
        ("9 / 19", "roles with no tools", PLUM),
        ("20", "writes that ask a human", AMBER),
    ])

    # ── the funnel: three transports → one module → allow / deny ──────────
    y = Inches(2.26)
    for i, (lab, sub) in enumerate([("Tool call", "web · Jira · MCP"),
                                    ("Shell command", "curl · scp · ssh"),
                                    ("Notebook cell", "kernel getaddrinfo")]):
        box(s, L, _e(y + i * Inches(0.56)), Inches(2.40), Inches(0.48), lab,
            sub, fill=SLATE_T, edge=SLATE, size=11, sub_size=8)
        arrow(s, Inches(3.02), _e(y + i * Inches(0.56) + Inches(0.24)),
              Inches(3.60), Inches(2.82))

    box(s, Inches(3.60), y, Inches(2.60), Inches(1.60), "net/egress.py",
        "one policy\nevery transport\nafter every redirect\n\n"
        "unlisted = DENIED", fill=BLUE_T, edge=BLUE, size=12, sub_size=8.5)

    arrow(s, Inches(6.20), Inches(2.66), Inches(6.86), Inches(2.66),
          color=GREEN)
    arrow(s, Inches(6.20), Inches(3.42), Inches(6.86), Inches(3.42),
          color=RED)
    box(s, Inches(6.86), Inches(2.42), Inches(2.72), Inches(0.50), "ALLOWED",
        "", fill=GREEN_T, edge=GREEN, size=10.5)
    chips(s, Inches(6.86), Inches(2.98), Inches(2.72),
          ["loopback · LAN · local registry", "git push to a named remote",
           "ssh to a declared deploy box"], fill=WHITE, edge=GREEN,
          color=GREEN)
    box(s, Inches(9.80), Inches(2.42), Inches(2.92), Inches(0.50), "DENIED",
        "", fill=RED_T, edge=RED, size=10.5)
    chips(s, Inches(9.80), Inches(2.98), Inches(2.92),
          ["scp · rsync · curl -d/-T · /dev/tcp",
           "aws s3 cp · docker push · npm publish",
           "web search — deleted outright"], fill=WHITE, edge=RED, color=RED)

    # ── the second row: who gets in, and what drives it ───────────────────
    y2 = Inches(4.34)
    box(s, L, y2, Inches(3.96), Inches(1.16), "Who gets in", "",
        fill=AMBER_T, edge=AMBER, size=11.5)
    chips(s, _e(L + Inches(0.16)), _e(y2 + Inches(0.38)), Inches(3.64),
          ["token on every /api/*  ·  CORS allow-list",
           "public bind + no token → refuses to boot"],
          fill=WHITE, edge=AMBER, color=AMBER)

    box(s, Inches(4.78), y2, Inches(3.96), Inches(1.16),
        "What drives it", "", fill=RED_T, edge=RED, size=11.5)
    chips(s, Inches(4.94), _e(y2 + Inches(0.38)), Inches(3.64),
          ["a ticket or a page is UNTRUSTED text",
           "so: containment, not detection"],
          fill=WHITE, edge=RED, color=RED)

    box(s, Inches(8.76), y2, Inches(3.96), Inches(1.16), "What a run costs",
        "", fill=GREEN_T, edge=GREEN, size=11.5)
    chips(s, Inches(8.92), _e(y2 + Inches(0.38)), Inches(3.64),
          ["rate ceiling per role  ·  request meter",
           "local first; cloud only after a failure"],
          fill=WHITE, edge=GREEN, color=GREEN)

    # ── the limits, stated ────────────────────────────────────────────────
    y3 = Inches(5.62)
    text(s, L, y3, Inches(3.0), Inches(0.24), "Limits we publish", size=10,
         bold=True, color=RED)
    for i, lab in enumerate([
            "the kernel guard is a guard, not a sandbox",
            "a shell can open a socket the policy never sees",
            "fleet sync is unauthenticated by default",
            "role tool filters fail OPEN, so a typo cannot brick an agent"]):
        chip(s, _e(L + (i % 2) * Inches(6.20)),
             _e(y3 + Inches(0.28) + (i // 2) * Inches(0.36)), Inches(5.92),
             lab, fill=WHITE, edge=RED, color=RED, size=9)

    notes(s, """
The incident that set the bar: a fetch the tool refused was rerouted through a
shell curl, then through a notebook cell, and it worked. A boundary you can
walk around by changing transport is a suggestion, so one module answers on all
three paths and after every redirect, and a missing allow-list entry is DENIED
rather than escalated to a human.

Outbound, refused: scp, rsync, sftp, ssh host 'cmd', curl -d/-F/-T, aws s3 cp,
docker push, npm publish, git push to a URL, and bash's own > /dev/tcp/host/port.
Inbound: web fetch, crawl, headless browser, @mention expansion. Web SEARCH was
deleted outright because the query string is our own data leaving the box, and
deleting the tool closed nothing until the refusal moved to the fetch layer.
AIFORGE_EGRESS_OFF closes everything in one switch; per-class switches cover
integrations, email, telemetry, MCP and fleet sync. Loopback, LAN, a local
registry and a git push to a named remote stay allowed on purpose — a control
that blocks ordinary work is a control that gets switched off.

Access: a token is required on every API route but health; a non-loopback bind
with NO token refuses to boot, and the guard inspects the real server rather
than an env var only run.sh sets. Loopback trust must be declared, because
behind a same-host reverse proxy every request looks like 127.0.0.1 — which was
a full auth bypass. CORS is an allow-list, never a wildcard. Limits: one shared
token, so no per-user identity or SSO yet, and fleet sync is open unless closed.

Untrusted input: a ticket description, a fetched page or a review comment
reaches the model beside your own instruction and we do not classify it. So the
answer is containment: the egress refusal is code rather than model judgement,
the 20 external-write tools ask a human, file tools are clamped to the
workspace, Plan mode cannot write, and unattended runs refuse writes outright.

Trust and secrets: no CERT_NONE anywhere — each endpoint's certificate is
verified, and one AIFORGE_CA_BUNDLE covers the model endpoint, the
integrations, AIForge's own HTTP and every subprocess (git, curl, npm), or a
self-signed host is pinned on first use. Credentials, MCP headers and the
runtime env live in one 0700 folder, files 0600, repaired on boot.

Cost: a rate ceiling in requests per minute per role keeps background work from
out-shouting a person, a request meter attributes every call to a role and a
task, long sessions compact into briefs, and escalation to a cloud model
happens only after a local failure and only if you registered one. With a local
endpoint the marginal cost of a run is electricity.

Supply chain: one build emits sonar/, blackduck/ and a CycloneDX SBOM; a heavy
dependency was dropped and its repo-map vendored under Apache-2.0, 212 packages
to 186. Every control above began as a bypass that WORKED in testing, and each
is pinned by a test verified to fail on the tree before the fix.
""")


# ------------------------------------------------------------- page 3 ------
def evidence_visual(prs):
    s = slide_base(
        prs, "The evidence, the gap, and the asks",
        "The zeros are the PLATFORM's, read off the scanner API. What they do "
        "not cover is the middle column — said here rather than left to find.",
        f"{AS_OF}. {M['ncloc']} lines, {M['files']} files, "
        f"{M['duplication']} duplication; one build feeds both scanner "
        "families and a hermetic Docker target runs the suite clean-room.")

    metric_strip(s, Inches(1.36), [
        (M["bugs"], "bugs", GREEN),
        (M["vulnerabilities"], "vulnerabilities", GREEN),
        (M["code_smells"], "code smells", GREEN),
        (M["ratings"], "Sonar ratings", GREEN),
        (M["tests"], "tests", BLUE),
        (M["coverage"], "coverage (86.7% pytest-cov)", BLUE),
    ])

    y = Inches(2.34)
    box(s, L, y, Inches(3.96), Inches(0.50), "MEASURED — the platform", "",
        fill=GREEN_T, edge=GREEN, size=11.5)
    chips(s, L, _e(y + Inches(0.58)), Inches(3.96), [
        "whole tree scanned, not two directories",
        "duplicate build copies excluded",
        "coverage regenerated with every scan",
        "9 vulnerabilities closed by CODE",
        "3 failures named, not rounded away",
    ], fill=WHITE, edge=GREEN, color=GREEN)

    box(s, Inches(4.68), y, Inches(3.96), Inches(0.50),
        "NOT MEASURED — the output", "", fill=RED_T, edge=RED, size=11.5)
    chips(s, Inches(4.68), _e(y + Inches(0.58)), Inches(3.96), [
        "no frozen ticket set, no replay",
        "no acceptance rate on generated MRs",
        "no edit distance to what shipped",
        "no load test → no sizing number",
        "no restore drill; traces name the box",
    ], fill=WHITE, edge=RED, color=RED)

    box(s, Inches(9.36), y, Inches(3.36), Inches(0.50),
        "REMOVED — versus the usual", "", fill=BLUE_T, edge=BLUE, size=11.5)
    chips(s, Inches(9.36), _e(y + Inches(0.58)), Inches(3.36), [
        "vector DB → markdown + SQLite",
        "opaque rows → git diff",
        "dump/restore → copy a folder",
        "vendor tool-calling → any model",
        "telemetry on → none in the lock",
    ], fill=WHITE, edge=BLUE, color=BLUE)

    y2 = Inches(4.94)
    text(s, L, y2, Inches(6.0), Inches(0.26), "Decisions we need from you",
         size=13, bold=True, color=INK)
    asks = [("Prove the output", "fund the harness above", RED),
            ("Name an owner", "one author is the bus factor", RED),
            ("Admin host", "which network holds company memory", BLUE),
            ("Boundary", "OS firewall rule + per-user identity", BLUE),
            ("Scanner profile", "make ours and the corporate one agree", BLUE)]
    w = Inches(2.34)
    for i, (lab, sub, col) in enumerate(asks):
        box(s, _e(L + i * (w + Inches(0.11))), Inches(5.28), w, Inches(0.86),
            lab, sub, fill=WHITE, edge=col, size=11, sub_size=8.5,
            label_color=col)

    notes(s, """
Why the zero is real: the scan covers the whole repository — an earlier config
scanned two directories, reported zero, and CI still failed. Duplicate build
copies are excluded so nothing is counted twice, and coverage is regenerated
with every scan because one out-of-range line makes the whole report read 0%.
The suite's own line coverage is 86.7%; Sonar reports 82.8% because it also
counts files the unit suite never imports. 8,606 tests ran: 3 failed, 0 errors,
0 skipped, all three in one file needing a model actually served on the shared
endpoint, which was empty — stated rather than rounded to zero. Nine standing
vulnerabilities were closed by CHANGING CODE (TLS pinning, scheme validation,
digest usage) after two earlier passes had triaged them as accepted decisions.
They were decisions; they were the wrong ones.

What the number does not say: it is the platform's quality, not the agent's
output. A tree with zero findings can still write a wrong patch. There is no
evaluation harness — no frozen set of closed tickets replayed each release, no
acceptance rate (merged without a rewrite), no edit distance between what the
agent wrote and what shipped, and nothing re-run when the model is swapped.
There is no load test, so no honest number for developers per box, VRAM for a
team of N, or wall-clock per ticket. Backup is copying one folder, but there is
no documented restore drill, and the traces that record every tool call name
the BOX rather than a person, with no retention or tamper-evidence: good for
debugging a run, not yet evidence for an auditor.

The comparison a budget actually faces: a per-seat assistant prices per
developer per month and sends the source to its vendor; this prices as hardware
you already own and sends it to the endpoint you set. Cost is the easy half —
output quality is the argument worth having, which is why the first ask is to
fund the harness.

The five asks, in order: prove the output; name an owner and a support rota;
decide which network holds the merged company memory and who owns it; add the
OS-level firewall rule the shell still needs and decide on per-user identity;
and confirm the corporate scanner profile so both servers agree.
""")


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    how_it_works(prs)
    containment_visual(prs)
    evidence_visual(prs)
    prs.save(OUT)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(prs.slides._sldIdLst)} slides)")


if __name__ == "__main__":
    build()
