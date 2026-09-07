"""Build docs/AIForgeCrew-CTO-Summary.pptx — a fourteen-page executive summary.

Audience: a CTO, not an engineer. Fourteen pages, one question each:

  1. What is it, and what does it replace? (product + shape)
  2. How does the work actually get done? (pipeline, chat modes, library)
  3. Does its OUTPUT hold up, and how would we know? (agent quality — open)
  4. What can it reach, and what holds it? (security + containment)
  5. Who can reach it, and whose words does it obey? (access + injection)
  6. Can our code or our data get out? (egress, both directions)
  7. Where do the source and the prompts actually go? (inference boundary)
  8. What does it give a team, and then the whole company? (shared memory)
  9. What does it cost to run, and how is it operated? (budget + deploy)
 10. How does it survive contact with production? (sizing, backup, upgrade)
 11. What is in the build, and what do we NOT claim? (supply chain + limits)
 12. What can audit, legal and model governance rely on? (record + licence)
 13. How does it compare with the usual stack? (what was removed)
 14. Is the code trustworthy, and what must you decide? (quality + asks)

Slides 3, 5, 10 and 12 exist to state what is NOT measured or NOT held. A deck
that only lists controls invites the audience to find the gap themselves, and
they will.

Run it:

    uv run --with python-pptx python docs/build_cto_summary_deck.py

Primitives are imported from build_overview_deck so the two decks stay one
visual system: change a colour there and both follow.

TWO RULES THIS FILE ENFORCES IN CODE, because both have burned a deck before:

* geometry goes through the sibling deck's ``_e`` helper — float EMU writes an
  invalid pptx that saves cleanly and PowerPoint then refuses;
* card bodies are NEVER hand-wrapped. ``card_row`` measures the text and sizes
  the box, so a sentence that grows cannot quietly spill past its border on a
  machine with no renderer to notice.

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
def security(prs):
    s = slide_base(
        prs, "Security — containment, not policy documents",
        "Self-hosted. Prompts go only to the endpoint you set; nothing phones home.",
        f"{AS_OF}. The limit is on the slide on purpose: this is a guard, not a "
        "sandbox — an OS egress firewall is still the outer boundary.")

    metric_strip(s, Inches(1.46), [
        (M["vulnerabilities"], "vulnerabilities", GREEN),
        (M["hotspots"], "security hotspots to review", GREEN),
        ("A", "Sonar security rating", GREEN),
        ("0", "unverified TLS calls in the tree", GREEN),
        ("0", "telemetry in a default install", GREEN),
    ])

    text(s, L, Inches(2.30), WD, Inches(0.52),
         "The incident that set the bar: a fetch refused by the tool was rerouted "
         "through a shell curl, then through a notebook cell — and it worked.\n"
         "A boundary you can walk around by changing transport is a suggestion, "
         "so the policy now sits under all three transports.",
         size=11.5, color=RED)

    y = Inches(2.98)
    for i, (lab, sub) in enumerate([
            ("Tool call", "web · browser · Jira\nGitLab · email · MCP"),
            ("Shell command", "curl · wget · nc · ssh\nscp · aws · git push"),
            ("Notebook cell", "in-kernel getaddrinfo\nand connect guard")]):
        x = L + i * Inches(3.05)
        box(s, x, y, Inches(2.85), Inches(0.78), lab, sub, fill=SLATE_T,
            edge=SLATE, size=12.5, sub_size=9)
        arrow(s, _e(x + Inches(1.42)), _e(y + Inches(0.78)), Inches(4.9),
              Inches(3.98))
    box(s, L, Inches(3.98), Inches(8.95), Inches(0.60),
        "One policy module  ·  net/egress.py",
        "the same answer on every path and after every redirect  ·  a missing "
        "allow-list entry is DENIED, never escalated to a human",
        fill=BLUE_T, edge=BLUE, size=13, sub_size=9.5)

    card_row(s, Inches(2.98), [
        ("Credentials", [
            "One 0700 folder, files 0600. Moved out of the old locations, "
            "never copied, and repaired on boot.",
        ], AMBER_T, AMBER),
    ], x0=Inches(9.78), total_w=Inches(2.94), min_h=Inches(1.60))

    card_row(s, Inches(4.76), [
        ("Transport trust", [
            "No CERT_NONE anywhere in the tree.",
            "Each endpoint's certificate is pinned and the hostname checked.",
            "Internal self-signed services still work; a swapped cert fails.",
        ], GREEN_T, GREEN),
        ("Blast radius", [
            "19 agent roles, 9 of them holding no tools at all.",
            "20 external-write tools ask a human by default.",
            "File tools are clamped to the workspace, on by default.",
        ], PLUM_T, PLUM),
        ("Unattended runs", [
            "Cron jobs and ticket pipelines have no approver to ask.",
            "So writes and dangerous commands are refused there, rather than "
            "auto-approved, unless an operator enables them.",
        ], RED_T, RED),
    ])


# ------------------------------------------------------------- page 2 ------
def nothing_leaves(prs):
    s = slide_base(
        prs, "Nothing leaves the system — both directions closed",
        "Pulling a page in and pushing our bytes out are the same question, "
        "answered by one module.",
        f"{AS_OF}. Every control here started as a bypass that WORKED in "
        "testing, and each is pinned by a test that fails on the unfixed tree.")

    card_row(s, Inches(1.52), [
        ("Code and data going OUT", [
            "scp · rsync · sftp · ssh host 'cmd' · nc host < file",
            "curl -d / -F / -T / -X POST and other upload flags",
            "aws s3 cp · gsutil cp · docker push · npm publish",
            "git push to a URL, and bash's own > /dev/tcp/host/port",
        ], RED_T, RED),
        ("Content coming IN", [
            "web fetch · crawl · headless browser · @mention expansion",
            "Web SEARCH was deleted outright: the query string is our own "
            "data, leaving the box.",
            "Deleting the tool closed nothing — any fetcher still took a "
            "search URL — so the refusal lives at the fetch layer.",
        ], AMBER_T, AMBER),
    ])

    for x in (Inches(3.5), Inches(9.7)):
        arrow(s, x, Inches(3.62), Inches(6.67), Inches(3.92))

    box(s, L, Inches(3.92), WD, Inches(0.58),
        "net/egress.py  —  one policy, three chokepoints",
        "the tool call  ·  the shell command line  ·  the notebook kernel's own "
        "getaddrinfo and connect",
        fill=BLUE_T, edge=BLUE, size=13, sub_size=9.5)

    card_row(s, Inches(4.70), [
        ("The switches an operator gets", [
            "AIFORGE_EGRESS_OFF — nothing leaves this box, page fetching "
            "included. One switch, not five.",
            "Per class: integration, email, telemetry, MCP, fleet sync.",
            "A host allow-list for headless and systemd installs.",
        ], SLATE_T, SLATE),
        ("Rules a write must pass", [
            "The host must be marked writable; one added in Settings is "
            "read-only until someone says otherwise.",
            "An unattended run has no approver, so it may not write at all "
            "unless that is explicitly enabled.",
        ], PLUM_T, PLUM),
        ("Refusals we deliberately did not add", [
            "Loopback and LAN, a local registry, a localstack endpoint.",
            "git push to a named remote; ssh to declared deploy boxes.",
            "A control that blocks ordinary work is a control that gets "
            "switched off.",
        ], GREEN_T, GREEN),
    ])


# ------------------------------------------------------------- page 3 ------
def where_code_goes(prs):
    s = slide_base(
        prs, "Where your code and your prompts actually go",
        "Point it at a model on your own hardware and the source never crosses "
        "the network at all.",
        f"{AS_OF}. There is no vendor account inside this product and no "
        "default cloud endpoint.")

    stops = [
        ("Your repo", "read in a git worktree,\nnot the working copy", WHITE, LINE),
        ("Agent", "file tools clamped to\nthe workspace; Plan\nmode cannot write",
         SLATE_T, SLATE),
        ("LLM client", "model registry\nrate ceiling\nrequest meter", BLUE_T, BLUE),
        ("Your endpoint", "LM Studio · vLLM · mlx\non your own hardware",
         GREEN_T, GREEN),
    ]
    for i, (lab, sub, f, e) in enumerate(stops):
        x = L + i * Inches(3.12)
        box(s, x, Inches(1.58), Inches(2.80), Inches(1.10), lab, sub, fill=f,
            edge=e, size=13, sub_size=9)
        if i < 3:
            arrow(s, _e(x + Inches(2.80)), Inches(2.13),
                  _e(x + Inches(3.12)), Inches(2.13))

    box(s, Inches(10.14), Inches(2.92), Inches(2.58), Inches(0.76),
        "Cloud model", "only if YOU register one, and only\nafter the local model "
        "failed the step", fill=AMBER_T, edge=AMBER, size=12, sub_size=8.5)
    arrow(s, Inches(11.43), Inches(2.68), Inches(11.43), Inches(2.92),
          color=AMBER, dashed=True)

    text(s, L, Inches(2.86), Inches(9.3), Inches(0.30),
         "Escalation to a cloud model happens on an actual failure — never on a "
         "timer, and never silently.", size=11, color=INK)

    card_row(s, Inches(3.86), [
        ("What never travels", [
            "Chat transcripts and raw captures stay on the machine.",
            "Memory captures and working notes stay too; only distilled "
            "knowledge nodes ever sync.",
            "Credentials are blocked by the redaction filter before a note is "
            "even offered to the admin.",
        ], GREEN_T, GREEN),
        ("What travels, and only where you point it", [
            "The prompt and its code context, to the endpoint you set.",
            "Embedding and rerank text, to the same endpoint.",
            "An optional Langfuse mirror — its own egress class, its own "
            "switch, off in a default install.",
        ], BLUE_T, BLUE),
        ("The edge we do not pretend to hold", [
            "A shell can open a socket the module never sees, and kernel code "
            "shares the process with its own guard.",
            "So the sandbox setting refuses host execution outright, and the "
            "outer boundary stays an OS egress firewall.",
        ], RED_T, RED),
    ])

    box(s, L, Inches(6.06), WD, Inches(0.46), "The short version",
        "with a local endpoint, an air-gapped install is the DEFAULT shape of "
        "this product, not a hardened variant of it",
        fill=WHITE, edge=BLUE, size=12, sub_size=10, label_color=BLUE)


# ------------------------------------------------------------- page 4 ------
def shared_memory(prs):
    s = slide_base(
        prs, "Shared memory — one engineer's lesson, everyone's next run",
        "Every machine learns locally. One admin merges. A filter runs before a "
        "note is ever offered.",
        f"{AS_OF}. Memory is a folder of markdown plus one SQLite file — no "
        "vector database to install, tune, back up or explain to an auditor.")

    for i, lab in enumerate(["Laptop", "Laptop", "CI / server"]):
        y = Inches(1.66) + i * Inches(0.70)
        box(s, L, y, Inches(1.85), Inches(0.54), lab, "compacts locally",
            fill=WHITE, edge=LINE, size=11.5, sub_size=8.5, label_color=INK)
        arrow(s, _e(L + Inches(1.85)), _e(y + Inches(0.27)), Inches(3.05),
              Inches(2.54))

    box(s, Inches(3.05), Inches(1.66), Inches(2.30), Inches(1.78),
        "Redaction", "runs on the CLIENT before\nanything is advertised\n\n"
        "secrets · private · noise\nblocks a note, never edits it",
        fill=AMBER_T, edge=AMBER, size=12.5, sub_size=8.5)
    arrow(s, Inches(5.35), Inches(2.54), Inches(6.05), Inches(2.54))

    box(s, Inches(6.05), Inches(1.66), Inches(2.70), Inches(1.78),
        "Team admin", "the ONLY merger\n\none fold per group,\nscoped trees, so two\n"
        "teams cannot mix", fill=BLUE_T, edge=BLUE, size=12.5, sub_size=8.5)
    arrow(s, Inches(8.75), Inches(2.54), Inches(9.45), Inches(2.54))

    box(s, Inches(9.45), Inches(1.66), Inches(3.27), Inches(1.78),
        "Company", "one admin serves many\nindependent fleets\n\ngroups are "
        "discovered from\nthe admin, never hardcoded",
        fill=GREEN_T, edge=GREEN, size=12.5, sub_size=8.5)

    text(s, L, Inches(3.56), WD, Inches(0.26),
         "What travels: distilled knowledge nodes only. Transcripts, captures "
         "and working notes never leave the machine that made them.",
         size=11, color=INK)

    card_row(s, Inches(3.96), [
        ("What the business gets", [
            "A fix found once is known fleet-wide the next morning.",
            "Onboarding reads the team's accumulated context, not a wiki.",
            "People leave; their working knowledge stays, in plain text.",
        ], GREEN_T, GREEN),
        ("What audit and legal get", [
            "Credential-shaped and private notes blocked at the source.",
            "Every merge is snapshotted and a revert endpoint restores it.",
            "Delete the folder and it is gone — there is no second copy.",
        ], PLUM_T, PLUM),
        ("What operations get", [
            "Nothing to run but the app; grep and git diff are the tools.",
            "Sync is spoke-initiated, so laptops behind NAT just work.",
            "Move a machine by copying a directory.",
        ], SLATE_T, SLATE),
    ])

    box(s, L, Inches(6.00), WD, Inches(0.48), "Deploy note",
        "the admin holds the merged fold — bind it to the LAN or a WireGuard "
        "network; the sync surface is open by default, on purpose",
        fill=WHITE, edge=RED, size=12, sub_size=10, label_color=RED)


# ------------------------------------------------------------- page 5 ------
def supply_chain(prs):
    s = slide_base(
        prs, "Supply chain, secrets at rest, and stated limits",
        "One build produces everything both scanner families need.",
        f"{AS_OF}. The limits on the right are printed in the product's own "
        "docs and env template, not only on this slide.")

    box(s, L, Inches(1.52), Inches(2.30), Inches(0.92), "One build",
        "test · package\nno second pipeline", fill=SLATE_T, edge=SLATE,
        size=13, sub_size=9)
    for i, (lab, sub, f, e) in enumerate([
            ("sonar/", "analysis config\njunit.xml\ncoverage.xml", BLUE_T, BLUE),
            ("blackduck/", "pinned requirements,\nprod and dev\nuv.lock · pyproject",
             GREEN_T, GREEN),
            ("SBOM", "CycloneDX, from the\nresolved environment", GREEN_T, GREEN),
            ("artifacts", "wheel · sdist\nweb package-lock", SLATE_T, SLATE)]):
        x = Inches(3.30) + i * Inches(2.42)
        arrow(s, _e(x - Inches(0.38)) if i else Inches(2.92), Inches(1.98),
              _e(x), Inches(1.98))
        box(s, x, Inches(1.52), Inches(2.30), Inches(0.92), lab, sub, fill=f,
            edge=e, size=13, sub_size=9)

    card_row(s, Inches(2.76), [
        ("Dependencies", [
            "One heavy agent dependency dropped and its repo-map component "
            "vendored under its Apache-2.0 licence: 212 to 186 packages.",
            "No telemetry SDK is required to run the product.",
        ], GREEN_T, GREEN),
        ("Secrets at rest", [
            "API keys, integration credentials, MCP headers and the runtime "
            "env in one 0700 folder, files 0600.",
            "Repaired on boot, because a write-time permission fix never "
            "reaches a file that already exists.",
        ], AMBER_T, AMBER),
        ("Limits we publish rather than imply", [
            "The kernel guard is a guard, not a sandbox.",
            "Shell commands can open sockets the policy never sees.",
            "The fleet-sync surface is unauthenticated by default.",
            "Role tool filters fail OPEN so a typo cannot brick an agent.",
        ], RED_T, RED),
    ])

    box(s, L, Inches(5.22), WD, Inches(1.12),
        "Why this list reads the way it does",
        "every item above began as a control that read correctly and did not "
        "hold: a switch honoured in two of the five places that read it, a "
        "filter whose one case-insensitive flag let every English word through, "
        "a permission fix that never touched an existing file, a refusal you "
        "could walk around by changing transport. They were found by attacking "
        "our own controls — and each fix is pinned by a test verified to fail "
        "on the tree before it.",
        fill=WHITE, edge=SLATE, size=12.5, sub_size=10.5)


# ------------------------------------------------------------- page 6 ------
def quality(prs):
    s = slide_base(
        prs, "Code quality — and the decisions we need from you",
        "Zero is the whole tree's number, not a subset's — and it is the "
        "PLATFORM's number, not the agent's output (slide 3).",
        f"{AS_OF}. Read off the scanner API, not inferred from a green "
        f"pipeline: {M['open_issues']} unresolved issues and {M['hotspots']} "
        f"hotspots awaiting review across {M['ncloc']} lines of production "
        f"code in {M['files']} files, {M['duplication']} duplication.")

    metric_strip(s, Inches(1.46), [
        (M["bugs"], "bugs", GREEN),
        (M["vulnerabilities"], "vulnerabilities", GREEN),
        (M["code_smells"], "code smells", GREEN),
        (M["ratings"], "all three ratings", GREEN),
        (M["tests"], "tests", BLUE),
        (M["coverage"], "coverage, as the scanner scopes it", BLUE),
    ])

    card_row(s, Inches(2.36), [
        ("Why the number is real", [
            "The scan covers the whole repository. An earlier config scanned "
            "two directories, reported zero, and CI still failed.",
            "Duplicate build copies excluded, so nothing is counted twice.",
            "Coverage regenerated with each scan: one out-of-range line makes "
            "the whole report read 0%.",
            f"The test suite's own line coverage is {M['coverage_raw']}; the "
            f"scanner reports {M['coverage']} because it also counts files the "
            "unit suite never imports.",
        ], BLUE_T, BLUE),
        ("The three failures, named", [
            f"{M['tests']} tests ran on the scanner box: {M['failures']} failed, "
            "0 errors, 0 skipped.",
            "All three are the same test file, and all three need a model "
            "actually served on the shared endpoint, which was empty.",
            "The last CI import to this same server showed 0 failures across "
            "8,667 tests — its container serves no model at all, so the tests "
            "never reach that path. Stated here rather than rounded to zero.",
        ], AMBER_T, AMBER),
        ("Fixed, not waived", [
            "Nine standing vulnerabilities were closed by changing code — TLS "
            "pinning, scheme validation, digest usage.",
            "Two earlier passes had triaged the same nine as accepted "
            "decisions. They were decisions; they were the wrong ones.",
            "The cleanup surfaced two real defects no finding had named.",
        ], GREEN_T, GREEN),
    ])

    box(s, L, Inches(4.42), WD, Inches(0.62), "Pipeline",
        "one GitLab build emits sonar/ (analysis, junit, coverage) and "
        "blackduck/ (dependency inventory and SBOM) for the corporate scanners  "
        "·  a hermetic Docker target runs the suite clean-room",
        fill=SLATE_T, edge=SLATE, size=12.5, sub_size=10)

    text(s, L, Inches(5.22), WD, Inches(0.28), "Decisions we need",
         size=13, bold=True, color=INK)
    card_row(s, Inches(5.48), [
        ("Prove the output", [
            "Fund the slide-3 harness. Nothing else settles the buy question.",
        ], WHITE, RED),
        ("Name an owner", [
            "One author today. A second maintainer, and a support rota.",
        ], WHITE, RED),
        ("Where the admin lives", [
            "One host holds the merged company memory. Which network?",
        ], WHITE, BLUE),
        ("Boundary and identity", [
            "The shell still needs an OS firewall rule; per-user identity is "
            "a decision, not a build.",
        ], WHITE, BLUE),
        ("Scanner profile", [
            "The corporate server runs analysers ours does not; confirm the "
            "profile so both agree.",
        ], WHITE, BLUE),
    ], body_size=9.5, head_size=11.5)


# ------------------------------------------------------------- page 1 ------
def what_it_is(prs):
    s = slide_base(
        prs, "AIForgeCrew — a coding agent platform you run yourself",
        "A ticket becomes a merge request. A question becomes work on the "
        "filesystem. Both on hardware you own.",
        f"{AS_OF}. One Python service, one port, one folder of state. No "
        "database to operate, no vendor account inside the product.")

    metric_strip(s, Inches(1.42), [
        (M["chat_tools"], "tools the chat agent drives", BLUE),
        (M["roles"], "specialised agent roles", BLUE),
        (M["ncloc"], "lines of production code", SLATE),
        (M["tests"], "tests", GREEN),
        ("0", "Sonar findings of any kind", GREEN),
    ])

    for i, (lab, sub, f, e) in enumerate([
            ("Ticket in", "Jira or GitLab issue picked up\nby the 19-role pipeline",
             BLUE_T, BLUE),
            ("Chat", "ask, plan or delegate — the\nagent works your filesystem",
             GREEN_T, GREEN),
            ("Schedule", "recurring jobs and pipelines\nwith no human in the loop",
             PLUM_T, PLUM)]):
        box(s, L + i * Inches(4.10), Inches(2.42), Inches(3.90), Inches(0.98),
            lab, sub, fill=f, edge=e, size=14, sub_size=9.5)
        arrow(s, _e(L + i * Inches(4.10) + Inches(1.95)), Inches(3.40),
              Inches(6.67), Inches(3.66))

    box(s, L, Inches(3.66), WD, Inches(0.62),
        "One FastAPI process  ·  port 8799  ·  React UI, REST and streaming",
        "chat engine  ·  agent pipeline  ·  memory  ·  integrations  ·  job "
        "scheduler — every one of them a library inside the same process",
        fill=SLATE_T, edge=SLATE, size=13, sub_size=9.5)
    arrow(s, Inches(6.67), Inches(4.28), Inches(6.67), Inches(4.54))
    box(s, L, Inches(4.54), WD, Inches(0.56),
        "Any OpenAI-compatible endpoint",
        "LM Studio · vLLM · mlx on your own hardware, or a cloud key you "
        "supply — the product ships with neither",
        fill=WHITE, edge=BLUE, size=13, sub_size=9.5, label_color=BLUE)

    card_row(s, Inches(5.28), [
        ("What it replaces", [
            "A per-seat coding assistant whose vendor sees your source.",
            "The vector-database stack an agent platform usually drags in.",
        ], GREEN_T, GREEN),
        ("What it needs", [
            "Python 3.12 and one model endpoint. No GPU required, no torch, "
            "no Postgres, no Neo4j, no message broker.",
        ], BLUE_T, BLUE),
        ("Where it runs", [
            "A laptop, a NUC or server under systemd, or a container — the "
            "same single command in each.",
        ], SLATE_T, SLATE),
    ])


# ------------------------------------------------------------- page 2 ------
def how_work_flows(prs):
    s = slide_base(
        prs, "How the work actually gets done",
        "Nineteen specialised roles driving the same tool surface, instead of "
        "one model doing everything.",
        f"{AS_OF}. Context gatherers run in parallel: researcher, a tree-sitter "
        "and PageRank repo map, and a conventions reader.")

    stages = ["Triage", "Enhancer", "Architect", "Planner", "Verifier"]
    bw, gap = Inches(2.26), Inches(0.20)
    for i, st in enumerate(stages):
        x = L + i * (bw + gap)
        box(s, x, Inches(1.52), bw, Inches(0.62), st, "", fill=BLUE_T,
            edge=BLUE, size=13)
        if i < len(stages) - 1:
            arrow(s, _e(x + bw), Inches(1.83), _e(x + bw + gap), Inches(1.83))

    box(s, L, Inches(2.34), WD, Inches(1.02),
        "Build loop — up to four parallel subtasks, each in its own git worktree",
        "Doer edits  ↔  tests run  ↔  Feedback reads the failures  ↔  Refiner "
        "fixes, all building against one spec written up front and reconciled "
        "at the end",
        fill=GREEN_T, edge=GREEN, size=13, sub_size=10)

    for i, (st, sub) in enumerate([("Validator", "gates the result"),
                                   ("Live-verifier", "runs the real recipe"),
                                   ("Learner", "writes memory back")]):
        x = L + i * Inches(4.10)
        arrow(s, _e(x + Inches(1.95)), Inches(3.36), _e(x + Inches(1.95)),
              Inches(3.58))
        box(s, x, Inches(3.58), Inches(3.90), Inches(0.66), st, sub,
            fill=PLUM_T, edge=PLUM, size=13, sub_size=9.5)

    box(s, L, Inches(4.38), WD, Inches(0.52), "Merge request",
        "the objectives, results and learnings the Learner writes are "
        "model-verified before they are saved",
        fill=SLATE_T, edge=SLATE, size=13, sub_size=9.5)

    card_row(s, Inches(5.08), [
        ("Three chat modes, one engine", [
            "Simple writes, Plan is read-only, Team runs the pipeline.",
            "Plain-text reasoning, so any model drives it — no vendor "
            "function-calling required.",
        ], GREEN_T, GREEN),
        ("A library that grows itself", [
            "Rules, skills and workflows are files the agent writes and reuses.",
            "A nightly sweep merges near-duplicates, so the prompt does not "
            "bloat by accretion.",
        ], AMBER_T, AMBER),
        ("Integrations", [
            f"Jira {M['jira_tools']} tools, Confluence {M['confluence_tools']}, "
            f"GitLab {M['gitlab_tools']}, plus GitHub, email and any MCP server.",
            "A tool with no credentials hides itself instead of failing a turn.",
        ], BLUE_T, BLUE),
    ])


# ------------------------------------------------------------- page 7 ------
def cost_and_ops(prs):
    s = slide_base(
        prs, "What it costs to run, and how it stays inside the budget",
        "The controls that stop an agent platform becoming an unbounded bill.",
        f"{AS_OF}. Escalation to a paid model is a documented event, not a "
        "default: it happens after a local failure and is metered.")

    for i, (lab, sub, f, e) in enumerate([
            ("Rate ceiling", "requests per minute per\nrole; background work\n"
                             "cannot out-shout a person", BLUE_T, BLUE),
            ("Request meter", "every call counted and\nattributed to a role\nand a task",
             BLUE_T, BLUE),
            ("Context compaction", "long sessions fold into\nbriefs instead of\n"
                                   "growing the prompt", GREEN_T, GREEN),
            ("Escalation policy", "local model first; a cloud\nmodel only after a real\n"
                                  "failure, never on a timer", AMBER_T, AMBER)]):
        box(s, L + i * Inches(3.08), Inches(1.52), Inches(2.88), Inches(1.16),
            lab, sub, fill=f, edge=e, size=13, sub_size=9)

    box(s, L, Inches(2.86), WD, Inches(0.54),
        "The practical consequence",
        "with a local endpoint the marginal cost of a run is electricity — "
        "the cloud key is a fallback you can leave unset",
        fill=WHITE, edge=BLUE, size=12.5, sub_size=10, label_color=BLUE)

    card_row(s, Inches(3.50), [
        ("Deploy", [
            "Laptop, a systemd unit on a server, or docker compose.",
            "One command in each; an unset admin URL means this box is it.",
        ], SLATE_T, SLATE),
        ("Operate", [
            "State is one folder; SQLite, not a server to cluster.",
            "A job scheduler runs recurring pipelines under the same "
            "unattended-write rules.",
        ], BLUE_T, BLUE),
        ("Observe", [
            "Structured JSON logs per role, ticket and event.",
            "Optional self-hosted Langfuse traces; every step replayable.",
        ], GREEN_T, GREEN),
    ])

    card_row(s, Inches(5.10), [
        ("It reads code structurally", [
            f"A tree-sitter repo map and {M['codegraph_tools']} knowledge-graph "
            "tools, so context is chosen, not pasted.",
        ], SLATE_T, SLATE),
        ("It can see a UI", [
            "A local vision model screenshots the running app and answers "
            "questions about it — no cloud vision service.",
        ], SLATE_T, SLATE),
        ("It runs the real toolchain", [
            "Persistent shell, language server, type-checker, test runner and "
            "an IPython kernel — not a simulated one.",
        ], SLATE_T, SLATE),
        ("It works while nobody watches", [
            "Scheduled ticket pipelines, nightly compaction and the sweep that "
            "merges duplicate rules — which is why those runs refuse writes.",
        ], PLUM_T, PLUM),
    ])


# ------------------------------------------------------------- page 8 ------
def versus(prs):
    s = slide_base(
        prs, "Compared with the usual agent stack",
        "Most of the operational burden here is burden we removed rather than "
        "documented.",
        f"{AS_OF}. The right column is the shipping default, not a hardened "
        "configuration you have to assemble. None of it depends on the network "
        "being off.")

    rows = [
        ("Storage", "a vector database to install, tune, back up",
         "markdown files plus one SQLite, in one folder"),
        ("Inspect", "query the database, decode embeddings",
         "cat, grep, an editor"),
        ("Version", "opaque rows", "git-diffable; snapshots and a revert endpoint"),
        ("Move a machine", "dump and restore", "copy a folder"),
        ("Delete", "hope the rows are gone", "delete the folder; there is no second copy"),
        ("Model", "vendor function-calling required",
         "plain-text reasoning — any model drives it"),
        ("Where data goes", "the vendor's endpoint by default",
         "the endpoint you set; local by default"),
        ("Telemetry", "usually on by default", "none, and no telemetry SDK in the lock"),
    ]
    hdr = Inches(1.50)
    for lab, x, w, col in [("", L, Inches(2.4), MUTED),
                           ("A typical agent stack", Inches(3.20), Inches(4.4), MUTED),
                           ("AIForgeCrew", Inches(7.90), Inches(4.8), BLUE)]:
        if lab:
            text(s, x, hdr, w, Inches(0.28), lab, size=11.5, bold=True, color=col)
    for i, (k, a, b) in enumerate(rows):
        y = Inches(1.88) + i * Inches(0.54)
        bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, _e(L), _e(y), _e(WD),
                                Inches(0.48))
        bg.fill.solid()
        bg.fill.fore_color.rgb = WHITE if i % 2 else SLATE_T
        bg.line.fill.background()
        bg.shadow.inherit = False
        text(s, _e(L + Inches(0.14)), _e(y + Inches(0.12)), Inches(2.3),
             Inches(0.28), k, size=11, bold=True)
        text(s, Inches(3.20), _e(y + Inches(0.12)), Inches(4.5), Inches(0.28),
             a, size=10.5, color=MUTED)
        text(s, Inches(7.90), _e(y + Inches(0.12)), Inches(4.7), Inches(0.28),
             b, size=10.5, color=INK)

    box(s, L, Inches(6.30), WD, Inches(0.56),
        "The comparison your budget actually faces",
        "a per-seat assistant — Copilot, Cursor, a Devin-class agent — prices "
        "per developer per month and sends the source to its vendor; this "
        "prices as hardware you already own and sends it to the endpoint you "
        "set. Cost is the easy half: the open question is output quality "
        "(slide 3), and it is the one worth arguing about.",
        fill=WHITE, edge=BLUE, size=12, sub_size=9.5, label_color=BLUE)



# ------------------------------------------------------------- page 3 ------
def does_it_work(prs):
    s = slide_base(
        prs, "Does the work it produces hold up?",
        "The zeros on the quality slide are OUR code. Whether a generated "
        "merge request is CORRECT is a different question, and unmeasured.",
        f"{AS_OF}. There is no evaluation harness in the tree: no golden ticket "
        "set, no acceptance rate, no evals directory. Stated here rather than "
        "left for you to assume from a green scan.")

    text(s, L, Inches(1.44), WD, Inches(0.46),
         "A platform with zero findings can still write a wrong patch. The "
         "quality slide says the code that drives the agent is sound; it says "
         "nothing about the agent's output. Nobody should conflate the two, so "
         "this deck separates them.", size=11.5, color=RED)

    h = card_row(s, Inches(2.02), [
        ("Measured today", [
            f"{M['tests']} tests, {M['coverage_raw']} line coverage and "
            f"{M['open_issues']} findings — over the platform's own source.",
            "Every containment control is pinned by a test verified to fail on "
            "the tree before the fix.",
            "That is a statement about the machinery, not about the patch it "
            "hands you.",
        ], BLUE_T, BLUE),
        ("NOT measured today", [
            "No frozen set of tickets replayed each release.",
            "No acceptance rate: how often a generated merge request is merged "
            "without a rewrite.",
            "No edit distance between what the agent wrote and what was merged.",
            "No re-run of any of the above when the model is swapped.",
        ], RED_T, RED),
        ("What holds the line meanwhile", [
            "Every run ends at a merge request a human reviews. Nothing merges "
            "itself.",
            "Validator gates the result and Live-verifier runs the real recipe "
            "before it is offered.",
            "Plan mode is read-only, and file tools cannot leave the workspace.",
        ], GREEN_T, GREEN),
    ])

    y = Inches(2.02) + h + Inches(0.24)
    card_row(s, y, [
        ("The harness we propose", [
            "A frozen set of already-closed tickets, replayed at each release.",
            "Pass = the repo's own tests pass AND a reviewer merges it without "
            "rewriting the diff.",
            "The same set re-run on any model or prompt change, so a swap "
            "cannot quietly regress the work.",
        ], AMBER_T, AMBER),
        ("Adoption evidence to collect", [
            "Tickets carried end to end, and the share that reach a merge "
            "request without a human rescue.",
            "Reviewer minutes per generated merge request.",
            "Per-role failure counts — already written to the trajectory files, "
            "never yet aggregated.",
        ], SLATE_T, SLATE),
        ("Why this is the deciding number", [
            "Containment decides whether we are ALLOWED to run it.",
            "Output quality decides whether it is WORTH running.",
            "Asking for this number is the correct response to the rest of "
            "this deck.",
        ], PLUM_T, PLUM),
    ])


# ------------------------------------------------------------- page 5 ------
def access_and_injection(prs):
    s = slide_base(
        prs, "Who can reach it, and whose words it obeys",
        "Egress answers where bytes go. These two answer who starts a run, and "
        "which text the model treats as an instruction.",
        f"{AS_OF}. Both controls below are code in the request path, not model "
        "judgement — a prompt cannot argue its way past either.")

    metric_strip(s, Inches(1.42), [
        ("token", "on every /api/* route but health", BLUE),
        ("refuses", "to boot on a public bind with no token", GREEN),
        ("never *", "CORS is an allow-list, not a wildcard", GREEN),
        ("1", "shared token — no per-user identity yet", AMBER),
        ("0", "injection classifiers on inbound text", RED),
    ])

    h = card_row(s, Inches(2.22), [
        ("Access — what holds", [
            "AIFORGE_API_TOKEN is required by every API route except health, "
            "as a header or a bearer.",
            "A non-loopback bind with no token REFUSES TO BOOT, and the guard "
            "inspects the real server rather than an env var only run.sh sets.",
            "Loopback trust is declared, and the admin surface never takes "
            "that shortcut.",
        ], GREEN_T, GREEN),
        ("Access — what does not", [
            "One shared token, so the trail names the BOX, not the person. No "
            "SSO, LDAP or per-user roles today.",
            "The fleet-sync surface is open unless AIFORGE_SYNC_AUTH is set.",
            "Found the hard way: behind a same-host reverse proxy every "
            "request looks like 127.0.0.1, so implicit loopback trust was a "
            "full auth bypass.",
        ], AMBER_T, AMBER),
    ])

    y = Inches(2.22) + h + Inches(0.18)
    box(s, L, y, WD, Inches(0.56),
        "The agent-specific risk: a Jira description, a fetched page or a "
        "review comment is UNTRUSTED TEXT that reaches the model beside your "
        "own instruction",
        "we do not mark provenance or classify it — so the answer is "
        "containment, not detection: the controls below do not consult the "
        "model before refusing",
        fill=RED_T, edge=RED, size=12.5, sub_size=9.5)

    card_row(s, _e(y + Inches(0.72)), [
        ("Why injected text still cannot do much", [
            "Egress policy is code: a refusal is not promptable.",
            "The 20 external-write tools ask a human first.",
            "File tools are clamped to the workspace; plan mode cannot write; "
            "unattended runs refuse writes outright.",
        ], GREEN_T, GREEN),
        ("What it could still cost you", [
            "Wasted runs, a misleading plan, a bad patch in front of a "
            "reviewer — caught by review, not by the machine.",
            "The reviewer stays in the loop for the same reason the writes "
            "ask: the model is not the control.",
        ], AMBER_T, AMBER),
        ("What would close it properly", [
            "Mark inbound content as data, not instruction, at the point it "
            "enters the prompt.",
            "Replay a set of hostile tickets in the same harness as slide 3.",
            "Neither exists yet; both are cheap next to the containment work "
            "already done.",
        ], BLUE_T, BLUE),
    ])


# ------------------------------------------------------------ page 10 ------
def run_in_production(prs):
    s = slide_base(
        prs, "Running it — sizing, failure, backup, upgrade",
        "One process, one port, one folder — plus the two operational questions "
        "we cannot yet answer with a number.",
        f"{AS_OF}. Everything on the left is measured from the code; the two "
        "amber cards are open, and both appear in the asks on the last slide.")

    for i, (lab, sub, f, e) in enumerate([
            ("One process", "uvicorn, default bind\n127.0.0.1:8799, no\nworker fan-out",
             SLATE_T, SLATE),
            ("One state folder", "AIFORGE_CONFIG_DIR —\nSQLite plus markdown,\n"
                                 "no server to cluster", BLUE_T, BLUE),
            ("Scheduler", "a daemon thread, cron\nexpressions, 30s tick,\n"
                          "AIFORGE_JOBS_DISABLE=1", PLUM_T, PLUM),
            ("Throughput knobs", "4 parallel subtasks in\nworktrees · analysis\n"
                                 "workers · an RPM ceiling", GREEN_T, GREEN)]):
        box(s, L + i * Inches(3.08), Inches(1.50), Inches(2.88), Inches(1.16),
            lab, sub, fill=f, edge=e, size=13, sub_size=9)

    h = card_row(s, Inches(2.86), [
        ("Failure modes, and what happens", [
            "Model endpoint down: the run fails visibly. There is no silent "
            "cloud fallback — escalation needs a model you registered.",
            "A bad edit lands in a git worktree, never the working copy.",
            "Missed schedules collapse: a three-day backlog fires once, "
            "because the next run is computed from now.",
        ], GREEN_T, GREEN),
        ("Sizing — OPEN", [
            "We have never load-tested it: developers per box, model VRAM for a "
            "team of N, wall-clock per ticket are all unmeasured.",
            "A single process with no worker fan-out is also the availability "
            "story: restart is the recovery.",
            "Do not let anyone quote you a rollout number before a load test.",
        ], AMBER_T, AMBER),
        ("Backup and upgrade", [
            "Upgrade is git pull and run.sh, which converges the environment "
            "and migrates the data in place.",
            "Backup is copying one folder — but there is no documented restore "
            "drill, retention or rotation. That is the honest gap.",
            "Rollback of a migrated state folder is untested.",
        ], AMBER_T, AMBER),
    ])

    box(s, L, _e(Inches(2.86) + h + Inches(0.22)), WD, Inches(0.50),
        "The shape of the ask",
        "neither open item is a design flaw — both are measurements nobody has "
        "paid for yet, and each is a day of work, not a quarter",
        fill=WHITE, edge=BLUE, size=12.5, sub_size=10, label_color=BLUE)


# ------------------------------------------------------------ page 12 ------
def governance(prs):
    s = slide_base(
        prs, "Audit, licence and model governance",
        "What the record shows, what it cannot show, and who owns the licence "
        "on the code the model writes.",
        f"{AS_OF}. The traces below are the product's default behaviour; the "
        "gaps are named so legal and audit hear them from us first.")

    h = card_row(s, Inches(1.46), [
        ("What is recorded", [
            "Every chat turn appends to a per-session trace: the message, EACH "
            "tool call with its arguments and outcome, and the reply — markdown "
            "for a human, JSONL beside it for a machine.",
            "The ticket pipeline dumps a full trajectory per run.",
            "Structured JSON logs per role, ticket and event; optional "
            "self-hosted Langfuse traces replay every step.",
        ], GREEN_T, GREEN),
        ("What the record cannot tell you", [
            "One shared token means the actor is the machine, not a named "
            "person — approvals included.",
            "Traces are files: no retention policy, no rotation, no "
            "tamper-evidence, and an env switch turns them off.",
            "So they are excellent for debugging a run and NOT yet evidence for "
            "an auditor. Naming that is cheaper than discovering it.",
        ], AMBER_T, AMBER),
    ])

    y = Inches(1.46) + h + Inches(0.24)
    card_row(s, y, [
        ("Licence and IP", [
            "The product is MIT. The vendored repo-map component keeps its "
            "Apache-2.0 licence and notice.",
            "The MODEL's licence is the operator's choice and it governs "
            "commercial use of what it writes — a local open-weights model is "
            "a legal decision, not only a cost one.",
            "Generated code carries no provenance stamp today; the merge "
            "request and its trace are the record.",
        ], PLUM_T, PLUM),
        ("Model governance", [
            "The registry pins each model: id, base URL, TLS and vision flags, "
            "keys held server-side and never returned to the UI.",
            "Roles pick a registered model by name, so a swap is one edit and "
            "is visible in config.",
            "But nothing re-validates BEHAVIOUR after a swap — the same harness "
            "from slide 3 is what closes this.",
        ], BLUE_T, BLUE),
        ("Ownership — the uncomfortable one", [
            "One author has written this platform. That is the bus factor.",
            "No support rota, no on-call, no published roadmap beyond the "
            "current work.",
            "A tool this central needs a named owner and a second pair of "
            "hands before it is depended on company-wide.",
        ], RED_T, RED),
    ])


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    what_it_is(prs)
    how_work_flows(prs)
    does_it_work(prs)
    security(prs)
    access_and_injection(prs)
    nothing_leaves(prs)
    where_code_goes(prs)
    shared_memory(prs)
    cost_and_ops(prs)
    run_in_production(prs)
    supply_chain(prs)
    governance(prs)
    versus(prs)
    quality(prs)
    prs.save(OUT)
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(prs.slides._sldIdLst)} slides)")


if __name__ == "__main__":
    build()
