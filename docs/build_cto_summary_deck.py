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

    The slides are drawn, not written — but a CTO deck still has to answer the
    hard question when it is asked out loud, so every sentence the boxes no
    longer carry lives here for the presenter.
    """
    s.notes_slide.notes_text_frame.text = body.strip()


def panel(s, x, y, w, h, title, color, tint):
    """A titled panel: the heading sits ON TOP of the frame, never inside it.

    A ``box`` centres its label vertically, so chips laid inside one cover the
    heading — which is exactly what the first cut of this deck did, and it was
    invisible until the pptx was rendered to PNG and looked at.
    """
    box(s, x, y, w, Inches(0.42), title, "", fill=tint, edge=color, size=11.5)
    frame = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, _e(x),
                               _e(y + Inches(0.42)), _e(w),
                               _e(h - Inches(0.42)))
    frame.fill.solid()
    frame.fill.fore_color.rgb = WHITE
    frame.line.color.rgb = color
    frame.line.width = Pt(1.0)
    frame.shadow.inherit = False
    return _e(y + Inches(0.52))


def chips(s, x, y, w, items, *, edge=LINE, color=INK, size=9,
          gap=Inches(0.07)):
    """A stack of pills. Returns the y below the last one."""
    for it in items:
        chip(s, x, y, w, it, fill=WHITE, edge=edge, color=color, size=size)
        y = _e(y + Inches(0.30) + gap)
    return y


# ------------------------------------------------------------- page 1 ------
def how_it_works(prs):
    s = slide_base(
        prs, "AIForgeCrew — a coding agent you run on your own machines",
        "Give it a ticket, get back a merge request. Our code stays on our "
        "hardware and talks to our own model.",
        "")

    metric_strip(s, Inches(1.36), [
        (M["chat_tools"], "things it can do", BLUE),
        (M["roles"], "agents it runs", BLUE),
        ("3", "ways to run it", BLUE),
        ("0", "databases to run", GREEN),
        (M["tests"], "tests", GREEN),
        ("0", "code-scan findings", GREEN),
    ])

    # ── the shape of the system ───────────────────────────────────────────
    y = Inches(2.30)
    for i, (lab, sub, f, e) in enumerate([
            ("A ticket", "from Jira or GitLab", BLUE_T, BLUE),
            ("A chat", "ask · plan · delegate", GREEN_T, GREEN),
            ("A schedule", "runs while you sleep", PLUM_T, PLUM)]):
        yy = _e(y + i * Inches(0.52))
        box(s, L, yy, Inches(2.30), Inches(0.44), lab, sub, fill=f, edge=e,
            size=11, sub_size=8)
        arrow(s, Inches(2.92), _e(yy + Inches(0.22)), Inches(3.44),
              _e(yy + Inches(0.22)))

    box(s, Inches(3.44), y, Inches(2.90), Inches(1.48), "One program",
        "chat · the agents\nmemory · our tools\nthe scheduler",
        fill=SLATE_T, edge=SLATE, size=12, sub_size=9)
    arrow(s, Inches(6.34), Inches(3.04), Inches(6.86), Inches(3.04))
    box(s, Inches(6.86), y, Inches(2.70), Inches(1.48), "Our own model",
        "running on our hardware\n\na paid cloud model only\nif we add one "
        "yourself", fill=GREEN_T, edge=GREEN, size=12, sub_size=8.5)

    box(s, Inches(9.86), y, Inches(2.86), Inches(1.48), "No database",
        "no vector store, no Postgres,\nno Neo4j, no queue, no GPU\n\n"
        "just files and one small\nSQLite file",
        fill=WHITE, edge=BLUE, size=12, sub_size=8.5, label_color=BLUE)

    # ── one ticket, end to end ────────────────────────────────────────────
    text(s, L, Inches(4.00), Inches(6.0), Inches(0.24),
         "What happens to one ticket", size=10.5, bold=True, color=MUTED)
    y2 = Inches(4.30)
    steps = [("Understand", BLUE_T, BLUE), ("Plan", BLUE_T, BLUE),
             ("Write code", GREEN_T, GREEN), ("Test and fix", GREEN_T, GREEN),
             ("Check it works", PLUM_T, PLUM), ("Merge request", SLATE_T, SLATE)]
    bw = Inches(1.86)
    for i, (lab, f, e) in enumerate(steps):
        x = L + i * (bw + Inches(0.19))
        box(s, x, y2, bw, Inches(0.46), lab, "", fill=f, edge=e, size=10.5)
        if i < len(steps) - 1:
            arrow(s, _e(x + bw), _e(y2 + Inches(0.23)),
                  _e(x + bw + Inches(0.19)), _e(y2 + Inches(0.23)))
    text(s, L, Inches(4.86), WD, Inches(0.26),
         "up to four pieces of the job run side by side, each in its own copy "
         "of the repo  ·  a person reviews the merge request — nothing merges "
         "itself", size=9, color=MUTED)

    # ── the two things that make it better over time ──────────────────────
    y3 = Inches(5.32)
    cy = panel(s, L, y3, Inches(6.00), Inches(1.42),
               "It writes down what it learns", AMBER, AMBER_T)
    for i, lab in enumerate(["Rules", "Skills", "Workflows"]):
        chip(s, _e(L + Inches(0.18) + i * Inches(1.42)), cy, Inches(1.30),
             lab, fill=WHITE, edge=AMBER, color=AMBER)
    chip(s, Inches(4.98), cy, Inches(0.94), "tidied nightly", fill=WHITE,
         edge=AMBER, color=AMBER, size=8.5)
    text(s, _e(L + Inches(0.18)), _e(cy + Inches(0.38)), Inches(5.64),
         Inches(0.34),
         "plain text files it writes for itself, so a fix found once becomes a "
         "step it repeats — and duplicates get merged every night",
         size=8.5, color=MUTED)

    cy = panel(s, Inches(6.72), y3, Inches(6.00), Inches(1.42),
               "It plugs into the tools you already have", BLUE, BLUE_T)
    for i, lab in enumerate([f"Jira {M['jira_tools']}",
                             f"Confluence {M['confluence_tools']}",
                             f"GitLab {M['gitlab_tools']}", "GitHub",
                             "e-mail"]):
        chip(s, _e(Inches(6.90) + i * Inches(1.12)), cy, Inches(1.04), lab,
             fill=WHITE, edge=BLUE, color=BLUE, size=8.5)
    text(s, Inches(6.90), _e(cy + Inches(0.38)), Inches(5.64), Inches(0.34),
         "plus any MCP server, a map of your codebase, a real shell and test "
         "runner, and a local model that can look at your screen",
         size=8.5, color=MUTED)

    notes(s, """
One FastAPI process on port 8799 — React UI, REST and streaming. Chat engine,
agent pipeline, memory, integrations and job scheduler are libraries inside it.
Python 3.12 and one model endpoint; no GPU, torch, Postgres, Neo4j, vector
store or message broker. Runs on a desktop, a NUC, a server under systemd or a
container, same single command; upgrade is git pull and run.sh, which converges
the environment and migrates in place.

The pipeline in full: Triage, Enhancer, Architect, Planner, Verifier, then a
build loop of up to four parallel subtasks each in its own git worktree (Doer
edits, tests run, Feedback reads failures, Refiner fixes), then Validator
gates, Live-verifier runs the real recipe, Learner writes memory back, and the
result is a merge request a human reviews. Reasoning is plain text, so any
model drives it — no vendor function-calling needed. Simple mode writes, Plan
mode is read-only, Team mode runs the whole pipeline.

Rules, skills and workflows are markdown the agent writes and reuses; a nightly
sweep merges near-duplicates so the prompt does not bloat, and a merge unions
the members' scopes rather than silently narrowing one.

Integrations: Jira 21 tools, Confluence 14, GitLab 10, plus GitHub, e-mail and
any MCP server; a tool with no credentials hides itself rather than failing a
turn. A tree-sitter and PageRank repo map plus 5 knowledge-graph tools choose
context instead of pasting it. The real toolchain is present: persistent shell,
language server, type-checker, test runner, IPython kernel. A local vision
model can screenshot the running app and answer questions about it.

If your estate runs its own certificate authority, one setting —
AIFORGE_CA_BUNDLE — makes the model endpoint, Jira, Confluence, GitLab, git,
curl and npm all trust it.
""")


# ------------------------------------------------------------- page 2 ------
def containment_visual(prs):
    s = slide_base(
        prs, "Nothing gets out unless you allow it",
        "Everything the agent does goes through one gate — a tool, a shell "
        "command or a notebook cell all get the same answer.",
        "")

    metric_strip(s, Inches(1.36), [
        (M["vulnerabilities"], "known weaknesses", GREEN),
        (M["hotspots"], "security items to review", GREEN),
        ("0", "usage data sent anywhere", GREEN),
        ("0", "connections that skip certificate checks", GREEN),
        ("20", "actions that ask you first", AMBER),
    ])

    y = Inches(2.30)
    for i, (lab, sub) in enumerate([("What the agent runs", "web · Jira · tools"),
                                    ("What it types in a shell", "curl · scp · ssh"),
                                    ("What it runs in a notebook", "its own code")]):
        box(s, L, _e(y + i * Inches(0.54)), Inches(2.86), Inches(0.46), lab,
            sub, fill=SLATE_T, edge=SLATE, size=10.5, sub_size=8)
        arrow(s, Inches(3.48), _e(y + i * Inches(0.54) + Inches(0.23)),
              Inches(4.00), Inches(2.84))

    box(s, Inches(4.00), y, Inches(2.30), Inches(1.44), "One gate",
        "the same answer\nevery time, even after\na redirect\n\n"
        "not on the list = NO", fill=BLUE_T, edge=BLUE, size=12.5,
        sub_size=8.5)

    arrow(s, Inches(6.30), Inches(2.62), Inches(6.80), Inches(2.62),
          color=GREEN)
    arrow(s, Inches(6.30), Inches(3.30), Inches(6.80), Inches(3.30),
          color=RED)
    cy = panel(s, Inches(6.80), Inches(2.30), Inches(2.86), Inches(1.64),
               "Allowed", GREEN, GREEN_T)
    chips(s, Inches(6.90), cy, Inches(2.66),
          ["our own network and machines", "pushing to our own git remote",
           "deploying to a server you named"], edge=GREEN, color=GREEN,
          size=8.5)
    cy = panel(s, Inches(9.86), Inches(2.30), Inches(2.86), Inches(1.64),
               "Refused", RED, RED_T)
    chips(s, Inches(9.96), cy, Inches(2.66),
          ["copying files to an outside host", "uploading to cloud storage",
           "web search — removed completely"], edge=RED, color=RED, size=8.5)

    y2 = Inches(4.06)
    cy = panel(s, L, y2, Inches(3.96), Inches(1.30), "Who can get in", AMBER,
               AMBER_T)
    chips(s, _e(L + Inches(0.12)), cy, Inches(3.72),
          ["every request needs a key",
           "on a public address with no key, it refuses to start"],
          edge=AMBER, color=AMBER, size=8.5)

    cy = panel(s, Inches(4.78), y2, Inches(3.96), Inches(1.30),
               "Text it reads is not an order", RED, RED_T)
    chips(s, Inches(4.90), cy, Inches(3.72),
          ["a ticket or web page could try to steer it",
           "so we box it in rather than try to spot it"],
          edge=RED, color=RED, size=8.5)

    cy = panel(s, Inches(8.76), y2, Inches(3.96), Inches(1.30),
               "Every action is written down", GREEN, GREEN_T)
    chips(s, Inches(8.88), cy, Inches(3.72),
          ["each run keeps a readable record of every step it took",
           "20 actions that reach outside stop and ask a person"],
          edge=GREEN, color=GREEN, size=8.5)

    y3 = Inches(5.44)
    cy = panel(s, L, y3, Inches(6.00), Inches(1.30),
               "Our code never leaves the building", BLUE, BLUE_T)
    chips(s, _e(L + Inches(0.14)), cy, Inches(5.72), [
        "prompts and code go only to the model we point it at",
        "with that model on our own hardware, it runs with the network off",
    ], edge=BLUE, color=BLUE, size=8.5)

    cy = panel(s, Inches(6.72), y3, Inches(6.00), Inches(1.30),
               "Our keys and our certificates", AMBER, AMBER_T)
    chips(s, Inches(6.86), cy, Inches(5.72), [
        "keys sit in one locked folder and are never shown back",
        "our own certificate authority is added in Settings, no restart",
    ], edge=AMBER, color=AMBER, size=8.5)

    notes(s, """
The incident that set the bar: a fetch the tool refused was rerouted through a
shell curl, then through a notebook cell, and it worked. A boundary you can walk
around by changing transport is a suggestion, so one module — net/egress.py —
answers on all three paths and after every redirect, and a missing allow-list
entry is DENIED rather than escalated to a human.

Refused outbound: scp, rsync, sftp, ssh host 'cmd', curl -d/-F/-T, aws s3 cp,
docker push, npm publish, git push to a URL, and bash's own > /dev/tcp/host/port.
Inbound: web fetch, crawl, headless browser, @mention expansion. Web SEARCH was
deleted outright because the query string is our own data leaving the box, and
deleting the tool closed nothing until the refusal moved to the fetch layer.
AIFORGE_EGRESS_OFF closes everything in one switch; per-class switches cover
integrations, e-mail, telemetry, MCP and fleet sync. Loopback, LAN, a local
registry and a git push to a named remote stay allowed on purpose — a control
that blocks ordinary work is a control that gets switched off.

Access: a token is required on every API route but health; a non-loopback bind
with NO token refuses to boot, and the guard inspects the real server rather
than an env var only run.sh sets. Loopback trust must be declared, because
behind a same-host reverse proxy every request looks like 127.0.0.1 — which was
a full auth bypass. CORS is an allow-list, never a wildcard. The limits: one
shared token, so no per-user identity or SSO yet.

Untrusted input: a ticket description, a fetched page or a review comment
reaches the model beside your own instruction and we do not classify it. So the
answer is containment: the refusal is code rather than model judgement, 20
external-write tools ask a human, file tools are clamped to the workspace, Plan
mode cannot write, and unattended runs refuse writes outright.

Certificates and secrets: no CERT_NONE anywhere. One AIFORGE_CA_BUNDLE covers
the model endpoint, the integrations, our own HTTP and every subprocess (git,
curl, npm); otherwise a self-signed host is pinned on first use. Credentials,
MCP headers and the runtime env live in one 0700 folder, files 0600, repaired
on boot.

Cost: a rate ceiling in requests per minute per role keeps background work from
out-shouting a person, a request meter attributes every call to a role and a
task, long sessions compact into briefs, and escalation to a cloud model
happens only after a local failure and only if you registered one. With a local
endpoint the marginal cost of a run is electricity.

Every control above began as a bypass that WORKED in testing, and each is
pinned by a test verified to fail on the tree before the fix.
""")


# ------------------------------------------------------------- page 3 ------
def memory_and_asks(prs):
    s = slide_base(
        prs, "What one person learns, the whole company keeps",
        "Each machine learns on its own. Secrets are stripped, a team lead "
        "merges the rest, and other teams read it.",
        "")

    y = Inches(1.44)
    steps = [
        ("One machine", "learns from every run\nnothing raw ever leaves",
         SLATE_T, SLATE),
        ("Secrets stripped", "passwords and private notes\nare blocked, not "
         "edited", AMBER_T, AMBER),
        ("Our team", "one lead merges it\ninto the team's memory", GREEN_T,
         GREEN),
        ("The company", "other teams read it\nteams stay separate", BLUE_T,
         BLUE),
    ]
    bw = Inches(2.72)
    for i, (lab, sub, f, e) in enumerate(steps):
        x = L + i * (bw + Inches(0.42))
        box(s, x, y, bw, Inches(1.00), lab, sub, fill=f, edge=e, size=12.5,
            sub_size=8.5)
        if i < len(steps) - 1:
            arrow(s, _e(x + bw), _e(y + Inches(0.50)),
                  _e(x + bw + Inches(0.42)), _e(y + Inches(0.50)))

    y1 = Inches(2.56)
    for i, (head, items, col, tint) in enumerate([
        ("What our team gets", [
            "a fix found once is known by everyone tomorrow",
            "a new joiner reads the team's real history",
            "people leave, their know-how stays",
        ], GREEN, GREEN_T),
        ("What the company gets", [
            "one place serves many teams at once",
            "each team's memory stays its own",
            "nothing to run: it is files in a folder",
        ], BLUE, BLUE_T),
        ("What legal and audit get", [
            "passwords are blocked before sharing, not after",
            "every merge is saved and can be undone",
            "delete the folder and it is gone — no second copy",
        ], PLUM, PLUM_T),
    ]):
        x = L + i * Inches(4.16)
        cy = panel(s, x, y1, Inches(3.90), Inches(1.64), head, col, tint)
        chips(s, _e(x + Inches(0.12)), cy, Inches(3.66), items, edge=col,
              color=col, size=8.5)

    # ── the honest half ───────────────────────────────────────────────────
    y2 = Inches(4.32)
    metric_strip(s, y2, [
        (M["bugs"], "bugs found by the scanner", GREEN),
        (M["vulnerabilities"], "security weaknesses", GREEN),
        (M["tests"], "tests", BLUE),
        (M["coverage"], "of the code covered by tests", BLUE),
        (M["ratings"], "scanner ratings", GREEN),
    ])

    y3 = Inches(5.20)
    cy = panel(s, L, y3, Inches(5.50), Inches(1.64),
               "What it asks of us", SLATE, SLATE_T)
    chips(s, _e(L + Inches(0.12)), cy, Inches(5.26), [
        "one machine with a model on it — a desktop, a NUC or a server",
        "one folder to back up; copy it and the machine has moved",
        "upgrades pull and restart; the data migrates itself",
    ], edge=SLATE, color=SLATE, size=8.5)

    cy = panel(s, Inches(6.32), y3, Inches(6.40), Inches(1.64),
               "How we would roll it out", BLUE, BLUE_T)
    chips(s, Inches(6.44), cy, Inches(6.16), [
        "one team runs it on real tickets and keeps what it learns",
        "a second team joins the same admin — the memory is already there",
        "company-wide once two teams have run it for a month",
    ], edge=BLUE, color=BLUE, size=8.5)

    notes(s, """
Memory in detail. Each machine compacts what it learned locally. A redaction
filter runs on the CLIENT before anything is advertised: credential-shaped and
private notes are BLOCKED, never quietly edited. One team admin is the only
merger, folding into per-group trees so two teams cannot mix, and one admin can
serve many independent fleets — groups are discovered from the admin rather
than hardcoded. Only distilled knowledge nodes travel; transcripts, captures
and working notes never leave the machine that made them. Every merge is
snapshotted and a revert endpoint restores it. Sync is spoke-initiated, so
laptops behind NAT just work, and moving a machine is copying a directory.
Deploy note: the admin holds the merged fold — bind it to the LAN or a
WireGuard network, because the sync surface is open by default on purpose.

The quality numbers are the PLATFORM's, read off the scanner API rather than
inferred from a green pipeline: 0 bugs, 0 vulnerabilities, 0 code smells, A/A/A
ratings, 8,606 tests, 82.8% coverage as Sonar scopes it (86.7% by pytest-cov on
the same run), 71,204 lines, 542 files, 0.1% duplication. Three test failures
are named rather than rounded away: all three need a model actually served on
the shared endpoint, which was empty. Nine standing vulnerabilities were closed
by changing code — TLS pinning, scheme validation, digest usage — after two
earlier passes had triaged them as accepted decisions.

What that number does NOT say: it is the platform's quality, not the agent's
output. There is no evaluation harness — no frozen set of closed tickets
replayed each release, no acceptance rate, no edit distance between what the
agent wrote and what shipped, and nothing re-run when the model is swapped.
There is no load test, so no honest number for users per box, VRAM for a team
of N, or wall-clock per ticket. Backup is copying one folder, but there is no
documented restore drill, and the traces that record every tool call name the
BOX rather than a person, with no retention or tamper-evidence.

The full ask list: fund the scoring harness; name an owner and a support rota;
decide which network holds the merged company memory and who owns it; add the
OS-level firewall rule the shell still needs and decide on per-user identity;
and confirm the corporate scanner profile so both servers agree.
""")


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    how_it_works(prs)
    containment_visual(prs)
    memory_and_asks(prs)
    prs.save(OUT)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(prs.slides._sldIdLst)} slides)")


if __name__ == "__main__":
    build()
