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


# ------------------------------------------------------------- page 1 ------
def what_and_how(prs):
    s = slide_base(
        prs, "AIForgeCrew — a coding agent platform you run yourself",
        "A ticket becomes a merge request; a question becomes work on the "
        "filesystem. One process, one folder of state, no database to operate.",
        f"{AS_OF}. Python 3.12 and one model endpoint. No GPU required, no "
        "torch, no Postgres, no Neo4j, no vector store, no message broker — "
        "the removals are the product as much as the features are.")

    metric_strip(s, Inches(1.40), [
        (M["chat_tools"], "tools the chat agent drives", BLUE),
        (M["roles"], "specialised agent roles", BLUE),
        ("3", "chat modes on one engine", BLUE),
        ("0", "databases to install or tune", GREEN),
        (M["ncloc"], "lines of production code", SLATE),
    ])

    box(s, L, Inches(2.24), WD, Inches(0.94),
        "Ticket · chat · schedule   →   one FastAPI process, React UI, REST "
        "and streaming   →   "
        "any OpenAI-compatible endpoint you point it at",
        "Triage → Enhancer → Architect → Planner → Verifier → a build loop of "
        "up to four parallel subtasks, each in its own git worktree (Doer edits "
        "↔ tests run ↔ Feedback reads failures ↔ Refiner fixes) → Validator "
        "gates → Live-verifier runs the real recipe → Learner writes memory "
        "back → merge request. Simple mode writes, Plan mode is read-only, Team "
        "mode runs the whole pipeline; reasoning is plain text, so any model "
        "drives it with no vendor function-calling.",
        fill=SLATE_T, edge=SLATE, size=12.5, sub_size=9.5)

    card_row(s, Inches(3.36), [
        ("It learns — and the learning is FILES", [
            "The Learner writes objectives, results and lessons back after "
            "every run, and they are model-verified before they are saved.",
            "Rules, skills and workflows are markdown the agent writes, reuses "
            "and edits — a fix found once becomes a reusable procedure, not a "
            "paragraph in someone's notes.",
            "A nightly sweep merges near-duplicate rules, skills and workflows, "
            "so the prompt does not bloat by accretion; merges union their "
            "scopes rather than silently narrowing one.",
            "Every artefact is a file: read it, git diff it, delete it.",
        ], AMBER_T, AMBER),
        ("Memory, with no database", [
            "A folder of markdown plus one SQLite file. Nothing to install, "
            "tune, back up or explain to an auditor.",
            "Each desktop compacts locally; a client-side redaction filter "
            "blocks credential-shaped and private notes BEFORE anything is "
            "advertised; one team admin merges; one admin serves many fleets.",
            "Only distilled knowledge nodes travel — transcripts, captures and "
            "working notes never leave the machine that made them.",
            "Every merge is snapshotted and a revert endpoint restores it; "
            "delete the folder and it is gone, there is no second copy.",
        ], GREEN_T, GREEN),
        ("What it can reach and read", [
            f"Jira {M['jira_tools']} tools, Confluence {M['confluence_tools']}, "
            f"GitLab {M['gitlab_tools']}, plus GitHub, email and any MCP "
            "server; a tool with no credentials hides itself.",
            f"A tree-sitter and PageRank repo map and {M['codegraph_tools']} "
            "knowledge-graph tools, so context is chosen, not pasted.",
            "The real toolchain: persistent shell, language server, "
            "type-checker, test runner and an IPython kernel.",
            "A local vision model screenshots the running app and answers "
            "questions about it — no cloud vision service.",
        ], BLUE_T, BLUE),
        ("Deploy and operate", [
            "A desktop, a NUC or a server under systemd, or a container — the "
            "same single command; an unset admin URL means this box is it.",
            "State is one folder; upgrade is git pull and run.sh, which "
            "converges the environment and migrates in place.",
            "A scheduler fires cron-shaped ticket pipelines, nightly memory "
            "compaction and the duplicate sweep, with no human in the loop.",
            "One process with no worker fan-out: restart is the recovery, and "
            "nobody has load-tested users-per-box yet.",
        ], PLUM_T, PLUM),
    ], head_size=11.5, body_size=8.8)


# ------------------------------------------------------------- page 2 ------
def containment(prs):
    s = slide_base(
        prs, "Containment, cost ceiling, and the limits we state out loud",
        "Self-hosted. Every control below began as a bypass that WORKED in "
        "testing, and each is pinned by a test that fails without the fix.",
        f"{AS_OF}. The limits in the last column are printed in the product's "
        "own docs and env template, not only on this slide: this is a guard, "
        "not a sandbox, and an OS egress firewall is still the outer boundary.")

    metric_strip(s, Inches(1.44), [
        (M["vulnerabilities"], "vulnerabilities", GREEN),
        (M["hotspots"], "security hotspots to review", GREEN),
        ("0", "telemetry in a default install", GREEN),
        ("0", "unverified TLS calls in the tree", GREEN),
        ("9 / 19", "agent roles holding no tools at all", PLUM),
    ])

    box(s, L, Inches(2.26), WD, Inches(0.70),
        "One policy module — net/egress.py — under all three transports: the "
        "tool call · the shell command line · the notebook kernel's own "
        "getaddrinfo and connect",
        "the bar was set by an incident: a fetch the tool refused was rerouted "
        "through a shell curl, then through a notebook cell, and it worked. A "
        "boundary you can walk around by changing transport is a suggestion — "
        "so a missing allow-list entry is now DENIED on every path and after "
        "every redirect, never escalated to a human.",
        fill=BLUE_T, edge=BLUE, size=12.5, sub_size=9.5)

    card_row(s, Inches(3.04), [
        ("Nothing leaves, either way", [
            "Out: scp · rsync · sftp · ssh host 'cmd' · curl -d/-F/-T · aws "
            "s3 cp · docker push · npm publish · git push to a URL · bash's "
            "own > /dev/tcp/host/port.",
            "In: web fetch, crawl, headless browser, @mention expansion. Web "
            "SEARCH was deleted outright — the query string is our own data "
            "leaving the box.",
            "AIFORGE_EGRESS_OFF closes everything in one switch; per-class "
            "switches cover integrations, email, telemetry, MCP and fleet sync.",
            "Deliberately still allowed: loopback and LAN, a local registry, "
            "git push to a named remote — a control that blocks ordinary work "
            "gets switched off.",
        ], RED_T, RED),
        ("Access, and untrusted input", [
            "A token is required on every API route but health, and a "
            "non-loopback bind with NO token refuses to boot. Loopback trust "
            "must be declared: behind a same-host reverse proxy every request "
            "looks like 127.0.0.1, which was a full auth bypass.",
            "CORS is an allow-list, never a wildcard. Limits: one shared "
            "token — no per-user identity or SSO yet — and fleet sync is open "
            "unless closed.",
            "A ticket description or a fetched page is UNTRUSTED TEXT reaching "
            "the model beside your instruction, and we do not classify it.",
            "So: containment, not detection. The egress refusal is code, the "
            "20 external-write tools ask a human, file tools are clamped to "
            "the workspace, Plan mode cannot write, and unattended runs refuse "
            "writes.",
        ], AMBER_T, AMBER),
        ("Where code goes · run cost", [
            "The prompt and its context go to the endpoint YOU set; with a "
            "local model an air-gapped install is the default shape of this "
            "product, not a hardened variant of it.",
            "Escalation to a cloud model happens only after a local failure, "
            "never on a timer, and only if you registered one.",
            "A rate ceiling in requests per minute per role keeps background "
            "work from out-shouting a person; a request meter attributes every "
            "call to a role and a task; long sessions compact into briefs.",
            "So the marginal cost of a run is electricity, and the cloud key "
            "is a fallback you can leave unset.",
        ], GREEN_T, GREEN),
        ("Secrets, supply chain, limits", [
            "API keys, integration credentials, MCP headers and the runtime "
            "env live in one 0700 folder, files 0600, repaired on boot — a "
            "write-time fix never reaches an existing file.",
            "No CERT_NONE anywhere: every certificate is pinned and its "
            "hostname checked.",
            "One build emits sonar/, blackduck/ and a CycloneDX SBOM; a heavy "
            "dependency was dropped and its repo-map vendored under "
            "Apache-2.0, 212 packages to 186.",
            "Published rather than implied: the kernel guard is a guard, not a "
            "sandbox; a shell can open a socket the policy never sees; fleet "
            "sync is unauthenticated by default; role tool filters fail OPEN so "
            "a typo cannot brick an agent.",
        ], SLATE_T, SLATE),
    ], head_size=11.5, body_size=8.3)


# ------------------------------------------------------------- page 3 ------
def evidence_and_asks(prs):
    s = slide_base(
        prs, "The evidence — and the decisions we need from you",
        "Zero is the whole tree's number, read off the SonarQube API, not "
        "inferred from a green pipeline. It describes the PLATFORM only.",
        f"{AS_OF}. {M['open_issues']} unresolved issues and {M['hotspots']} "
        f"hotspots across {M['ncloc']} lines in {M['files']} files, "
        f"{M['duplication']} duplication. One GitLab build emits sonar/ "
        "(analysis, junit, coverage) and blackduck/ (inventory, SBOM) for the "
        "corporate scanners; a hermetic Docker target runs the suite clean-room.")

    metric_strip(s, Inches(1.44), [
        (M["bugs"], "bugs", GREEN),
        (M["vulnerabilities"], "vulnerabilities", GREEN),
        (M["code_smells"], "code smells", GREEN),
        (M["ratings"], "all three Sonar ratings", GREEN),
        (M["tests"], "tests", BLUE),
        (M["coverage"], "coverage as Sonar scopes it", BLUE),
    ])

    card_row(s, Inches(2.36), [
        ("Why the zero is real", [
            "The scan covers the whole repository. An earlier config scanned "
            "two directories, reported zero, and CI still failed.",
            "Duplicate build copies are excluded, so nothing is counted twice, "
            "and coverage is regenerated with every scan — one out-of-range "
            f"line makes the whole report read 0%. The suite's own line "
            f"coverage is {M['coverage_raw']}; Sonar reports {M['coverage']} "
            "because it also counts files the unit suite never imports.",
            f"{M['tests']} tests ran: {M['failures']} failed, 0 errors, 0 "
            "skipped. All three are the same file and all three need a model "
            "actually served on the shared endpoint, which was empty — stated "
            "rather than rounded to zero.",
            "Nine standing vulnerabilities were closed by CHANGING CODE — TLS "
            "pinning, scheme validation, digest usage — after two earlier "
            "passes had triaged them as accepted decisions. They were "
            "decisions; they were the wrong ones.",
        ], GREEN_T, GREEN),
        ("What that number does NOT say", [
            "It is the platform's quality, not the agent's output. A tree with "
            "zero findings can still write a wrong patch.",
            "There is no evaluation harness: no frozen set of closed tickets "
            "replayed each release, no acceptance rate (merged without a "
            "rewrite), no edit distance between what the agent wrote and what "
            "shipped, and nothing re-run when the model is swapped.",
            "No load test, so no honest number for developers per box, VRAM "
            "for a team of N, or wall-clock per ticket.",
            "Backup is copying one folder, but there is no documented restore "
            "drill; traces record every tool call yet name the BOX, not a "
            "person, with no retention or tamper-evidence — good for debugging "
            "a run, not yet evidence for an auditor.",
        ], RED_T, RED),
        ("Against the usual agent stack", [
            "A vector database to install, tune and back up → markdown plus one "
            "SQLite in one folder; cat, grep and an editor instead of decoding "
            "embeddings; git-diffable instead of opaque rows.",
            "Dump-and-restore to move a machine → copy a folder. Hope the rows "
            "are gone → delete the folder.",
            "Vendor function-calling required → plain-text reasoning any model "
            "drives. The vendor's endpoint by default → the endpoint you set. "
            "Telemetry usually on → none, and no telemetry SDK in the lock.",
            "The comparison a budget faces: a per-seat assistant prices per "
            "developer per month and sends the source to its vendor; this "
            "prices as hardware you own. Cost is the easy half — output "
            "quality is the argument worth having.",
        ], BLUE_T, BLUE),
        ("Decisions we need", [
            "PROVE THE OUTPUT — fund the harness above. Nothing else settles "
            "the buy question, and it is days of work, not a quarter.",
            "NAME AN OWNER — one author has written this platform. A second "
            "maintainer and a support rota before it is depended on "
            "company-wide.",
            "WHERE THE ADMIN LIVES — one host holds the merged company memory. "
            "Which network, and who owns it?",
            "BOUNDARY AND IDENTITY — three transports are held in-process; the "
            "shell still needs an OS firewall rule, and per-user identity is a "
            "decision, not a build.",
            "SCANNER PROFILE — the corporate server runs analysers ours does "
            "not; confirm the profile so both agree.",
        ], PLUM_T, PLUM),
    ], head_size=11.5, body_size=8.8)


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    what_and_how(prs)
    containment(prs)
    evidence_and_asks(prs)
    prs.save(OUT)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(prs.slides._sldIdLst)} slides)")


if __name__ == "__main__":
    build()
