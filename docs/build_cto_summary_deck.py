"""Build docs/AIForgeCrew-CTO-Summary.pptx — four pages, drawn properly.

This generator deliberately does NOT import the older deck's box/chip
primitives. Four rounds of edits proved that the moment you have `box` and
`chip`, every page becomes a grid of rounded rectangles no matter what the
content is; the fifth round asked for a visual answer instead, so the shapes
here are built for the ideas they carry:

  1. THREE PRESSURES — why we are doing this at all: a speed gap, a leaking
     perimeter with the unapproved tools drawn inside it, and an uneven lift,
     converging on one dark bar that says what we build about it.
  2. a HUB — one program at the centre, work coming in on the left, the model
     and the tools on the right, and the ticket's journey as a chevron run
     underneath. The claim of the page is "one thing, in the middle, that you
     own", and the drawing says it before the words do.
  3. RINGS — containment is layers, so it is drawn as layers: the code at the
     core, the workspace and approvals around it, the one gate around that,
     and the firewall we do NOT provide as the outermost ring, greyed, because
     stating the boundary we don't hold is the honest part.
  4. GROWTH — memory as three widening circles, one machine to a team to the
     company, with the redaction gate sitting ON the ring it guards.

Everything is measured on main and dated in the notes. Run it:

    uv run --with python-pptx python docs/build_cto_summary_deck.py

Rules learned the hard way and enforced here:

* geometry goes through ``_e`` — a float EMU writes a pptx that saves cleanly
  and PowerPoint then refuses to open;
* text is never hand-wrapped; ``fit_lines`` measures it and the caller sizes
  the shape;
* RENDER AND LOOK. A pass from the geometry checker meant nothing the day two
  headings turned out to be hidden behind the chips drawn over them.
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

OUT = Path(__file__).resolve().parent / "AIForgeCrew-CTO-Summary.pptx"

W, H = Inches(13.333), Inches(7.5)
L, WD = Inches(0.62), Inches(12.09)
FONT = "Segoe UI"

# ── palette ────────────────────────────────────────────────────────────────
NAVY = RGBColor(0x0B, 0x1B, 0x2B)
NAVY_2 = RGBColor(0x18, 0x3B, 0x5C)
INK = RGBColor(0x1A, 0x20, 0x2C)
MUTED = RGBColor(0x64, 0x74, 0x8B)
FAINT = RGBColor(0x94, 0xA3, 0xB8)
LINE = RGBColor(0xE2, 0xE8, 0xF0)
MIST = RGBColor(0xF6, 0xF8, 0xFB)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

BLUE = RGBColor(0x2B, 0x6C, 0xB0)
BLUE_T = RGBColor(0xEA, 0xF2, 0xFB)
GREEN = RGBColor(0x2F, 0x7A, 0x57)
GREEN_T = RGBColor(0xE8, 0xF5, 0xEE)
PLUM = RGBColor(0x6B, 0x46, 0xC1)
PLUM_T = RGBColor(0xF0, 0xEB, 0xFB)
AMBER = RGBColor(0xB7, 0x79, 0x1F)
AMBER_T = RGBColor(0xFD, 0xF3, 0xE2)
RED = RGBColor(0xC5, 0x30, 0x30)
RED_T = RGBColor(0xFB, 0xEC, 0xEC)
TEAL = RGBColor(0x2C, 0x7A, 0x7B)
TEAL_T = RGBColor(0xE6, 0xF4, 0xF4)

MEASURED = "measured on main @ 7d9e60ae, 2026-09-06"
M = {"tools": "108", "roles": "19", "tests": "8,606", "coverage": "82.8%",
     "ncloc": "71,204", "jira": "21", "confluence": "14", "gitlab": "10"}

_EM, _LEAD = 0.50, 1.28


def _e(v) -> Emu:
    """Every coordinate goes through here: Inches(x)/2 is a FLOAT, and a float
    EMU writes `x="1988820.0"`, which saves fine and PowerPoint refuses."""
    return Emu(int(v))


def fit_lines(s: str, width_in: float, size: float) -> int:
    per = max(1, int(width_in / (size * _EM / 72.0)))
    total = 0
    for para in s.split("\n"):
        n, line = 1, ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if len(trial) <= per:
                line = trial
            else:
                n += 1
                line = word
        total += n
    return total


def text_h(s: str, width_in: float, size: float) -> float:
    return fit_lines(s, width_in, size) * size * _LEAD / 72.0


# ── primitives ─────────────────────────────────────────────────────────────
def txt(s, x, y, w, h, body, *, size=12, color=INK, bold=False,
        align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, italic=False,
        spacing=0.0):
    box = s.shapes.add_textbox(_e(x), _e(y), _e(w), _e(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    lines = body.split("\n")
    tf.text = lines[0]
    for extra in lines[1:]:
        tf.add_paragraph().text = extra
    for p in tf.paragraphs:
        p.alignment = align
        if spacing:
            p.space_after = Pt(spacing)
        for r in p.runs:
            r.font.name = FONT
            r.font.size = Pt(size)
            r.font.bold = bold
            r.font.italic = italic
            r.font.color.rgb = color
    return box


def shape(s, kind, x, y, w, h, *, fill=None, edge=None, edge_w=1.0,
          gradient=None, angle=0.0, adjust=None):
    sp = s.shapes.add_shape(kind, _e(x), _e(y), _e(w), _e(h))
    if gradient:
        # SOLID, not a gradient. python-pptx writes a valid <a:gradFill>, but
        # a deck carrying them would not open on the reviewer's Mac while the
        # same deck without them did — and a page nobody can open is worth
        # less than a page with a flat header. Depth comes from stacking two
        # solids instead (see `band`), which every renderer agrees on.
        sp.fill.solid()
        sp.fill.fore_color.rgb = gradient[0]
    elif fill is None:
        sp.fill.background()
    else:
        sp.fill.solid()
        sp.fill.fore_color.rgb = fill
    if edge is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = edge
        sp.line.width = Pt(edge_w)
    sp.shadow.inherit = False
    if adjust is not None:
        for i, v in enumerate(adjust):
            try:
                sp.adjustments[i] = v
            except (IndexError, KeyError):
                pass
    sp.text_frame.word_wrap = True
    return sp


def label(sp, body, *, size=12, color=WHITE, bold=True, align=PP_ALIGN.CENTER):
    tf = sp.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    lines = body.split("\n")
    tf.text = lines[0]
    for extra in lines[1:]:
        tf.add_paragraph().text = extra
    for i, p in enumerate(tf.paragraphs):
        p.alignment = align
        for r in p.runs:
            r.font.name = FONT
            r.font.bold = bold if i == 0 else False
            r.font.size = Pt(size if i == 0 else size * 0.72)
            r.font.color.rgb = color
    return sp


def line(s, x1, y1, x2, y2, *, color=LINE, width=1.25, dashed=False):
    from pptx.oxml.ns import qn
    conn = s.shapes.add_connector(1, _e(x1), _e(y1), _e(x2), _e(y2))
    conn.line.color.rgb = color
    conn.line.width = Pt(width)
    if dashed:
        ln = conn.line._get_or_add_ln()
        ln.insert(0, ln.makeelement(qn("a:prstDash"), {"val": "sysDash"}))
    return conn


def arrow(s, x1, y1, x2, y2, *, color=FAINT, width=1.5, dashed=False):
    from pptx.oxml.ns import qn
    conn = s.shapes.add_connector(1, _e(x1), _e(y1), _e(x2), _e(y2))
    conn.line.color.rgb = color
    conn.line.width = Pt(width)
    ln = conn.line._get_or_add_ln()
    ln.append(ln.makeelement(qn("a:tailEnd"),
                             {"type": "triangle", "w": "med", "len": "med"}))
    if dashed:
        ln.insert(0, ln.makeelement(qn("a:prstDash"), {"val": "sysDash"}))
    return conn


# ── page furniture ─────────────────────────────────────────────────────────
_page = 0


def page(prs, title, kicker, *, accent=BLUE, stats=()):
    """A dark gradient hero band with the title, and optional stat orbs.

    The band is the deck's spine: three pages that open the same way read as
    one document, and white-on-navy buys the contrast that a page full of
    tinted cards never had.
    """
    global _page
    _page += 1
    s = prs.slides.add_slide(prs.slide_layouts[6])

    shape(s, MSO_SHAPE.RECTANGLE, 0, 0, W, Inches(1.62), fill=NAVY)
    shape(s, MSO_SHAPE.RECTANGLE, Inches(8.20), 0, _e(W - Inches(8.20)),
          Inches(1.62), fill=NAVY_2)
    shape(s, MSO_SHAPE.RECTANGLE, Inches(7.70), 0, Inches(0.50), Inches(1.62),
          fill=RGBColor(0x11, 0x2B, 0x44))
    # a hairline of the page's accent along the bottom of the band
    shape(s, MSO_SHAPE.RECTANGLE, 0, Inches(1.60), W, Pt(3), fill=accent)

    # The title is SIZED TO FIT ON ONE LINE, and the width it has to fit in
    # depends on how many stat orbs sit beside it. Two rounds were lost to a
    # title that wrapped and printed its second line straight through the
    # kicker: bold Segoe is nearer 0.60em than the 0.50 that prose averages,
    # so guessing a fixed size only moves the collision around.
    orbs_w = (len(stats) * 0.86 + max(len(stats) - 1, 0) * 0.22
              + 0.30) if stats else 0.0
    avail = 13.333 - 0.62 - 0.62 - orbs_w
    size = next((pt for pt in (24, 22, 20, 18, 16)
                 if len(title) * pt * 0.60 / 72.0 <= avail), 15)
    txt(s, L, Inches(0.34), Inches(avail), Inches(0.56), title,
        size=size, bold=True, color=WHITE)
    txt(s, L, Inches(0.98), Inches(avail), Inches(0.46), kicker,
        size=11.5, color=RGBColor(0xB8, 0xC7, 0xD9))

    # stat orbs, right-aligned inside the band
    if stats:
        d = Inches(0.86)
        gap = Inches(0.22)
        total = len(stats) * d + (len(stats) - 1) * gap
        x = W - Inches(0.62) - total
        for value, cap, col in stats:
            shape(s, MSO_SHAPE.OVAL, x, Inches(0.30), d, d,
                  fill=RGBColor(0x14, 0x2C, 0x45), edge=col, edge_w=1.5)
            txt(s, x, Inches(0.46), d, Inches(0.34), value, size=15,
                bold=True, color=WHITE, align=PP_ALIGN.CENTER)
            txt(s, _e(x - Inches(0.16)), Inches(1.22), _e(d + Inches(0.32)),
                Inches(0.30), cap, size=8, color=RGBColor(0x9F, 0xB3, 0xC8),
                align=PP_ALIGN.CENTER)
            x = _e(x + d + gap)

    txt(s, Inches(12.6), Inches(7.06), Inches(0.5), Inches(0.3), str(_page),
        size=9.5, color=FAINT, align=PP_ALIGN.RIGHT)
    return s


def notes(s, body):
    s.notes_slide.notes_text_frame.text = body.strip()


def bullet(s, x, y, w, glyph, head, body, color, *, size=10.5):
    """A tinted disc with a glyph, a bold lead-in and a line of prose.

    No card, no border: the disc carries the colour, which is what stops a
    column of these reading as another table.
    """
    d = Inches(0.30)
    shape(s, MSO_SHAPE.OVAL, x, y, d, d, fill=color)
    txt(s, x, _e(y + Inches(0.035)), d, Inches(0.24), glyph, size=10,
        bold=True, color=WHITE, align=PP_ALIGN.CENTER)
    inner = (w - Inches(0.42)) / Inches(1)
    h = Inches(text_h(f"{head} {body}", inner, size))
    t = txt(s, _e(x + Inches(0.42)), _e(y - Inches(0.02)),
            _e(w - Inches(0.42)), h, f"{head} {body}", size=size)
    # bold just the lead-in
    p = t.text_frame.paragraphs[0]
    if p.runs:
        r = p.runs[0]
        r.text = head + " "
        nxt = p.add_run()
        nxt.text = body
        nxt.font.name, nxt.font.size = FONT, Pt(size)
        nxt.font.color.rgb = INK
        r.font.bold = True
        r.font.color.rgb = color
    return max(h, d)


# ── dashed outline, for the perimeter we do not actually hold ──────────────
def dash_edge(sp):
    """Make an autoshape's outline dashed. Appended AFTER the solid fill the
    caller already set: a:ln's children are an ordered sequence, so inserting
    at 0 puts prstDash before the fill and writes a technically invalid part.
    """
    from pptx.oxml.ns import qn
    ln = sp.line._get_or_add_ln()
    ln.append(ln.makeelement(qn("a:prstDash"), {"val": "sysDash"}))
    return sp


# ────────────────────────────────────────────────── page 1: why at all ─────
def page_why(prs):
    """THREE PRESSURES, then one answer.

    Not a hub, not rings: three columns that each carry their own small
    drawing (a speed gap, a leaking perimeter, an uneven lift), three arrows
    converging, and a single dark bar that says what we do about it. The
    shapes are deliberately unlike the other pages' — this page argues, the
    others describe.
    """
    s = page(prs, "Why we are building this",
             "The question is not whether the company uses AI. It already "
             "does — through tools nobody approved, logged or paid for.",
             accent=AMBER)

    cw, ch = Inches(3.89), Inches(2.62)
    cy = Inches(2.02)
    xs = [Inches(0.62), Inches(4.72), Inches(8.82)]
    cols = [AMBER, RED, BLUE]
    heads = ["Standing still is now the risk",
             "It is here — but not through us",
             "The gain is real, but uneven"]
    bodies = [
        "Teams shipping with AI are pulling ahead, and our engineers know "
        "it. They are not waiting for us to decide.",
        "Personal accounts, browser tools, code pasted into a chat box. We "
        "cannot name the tools, the data or the spend.",
        "Whoever found a tool got faster. None of it is shared, repeatable "
        "or measurable across the team.",
    ]

    for i, (x, col, head, body) in enumerate(zip(xs, cols, heads, bodies)):
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, cy, cw, ch,
              fill=WHITE, edge=col, edge_w=1.25, adjust=[0.05])
        d = Inches(0.30)
        shape(s, MSO_SHAPE.OVAL, _e(x + Inches(0.22)), _e(cy + Inches(0.20)),
              d, d, fill=col)
        txt(s, _e(x + Inches(0.22)), _e(cy + Inches(0.235)), d, Inches(0.24),
            str(i + 1), size=10, bold=True, color=WHITE,
            align=PP_ALIGN.CENTER)
        inner = cw - Inches(0.84)
        txt(s, _e(x + Inches(0.62)), _e(cy + Inches(0.18)), inner,
            Inches(0.48), head, size=11.5, bold=True, color=col)

        # each column's own small drawing, in a fixed band
        zx, zy = _e(x + Inches(0.22)), _e(cy + Inches(0.86))
        zw = cw - Inches(0.44)

        if i == 0:
            # a speed gap: one long bar, one short one
            txt(s, zx, zy, zw, Inches(0.22), "HOW FAST WORK SHIPS",
                size=8, bold=True, color=FAINT)
            track = Inches(2.52)
            bx = _e(zx + Inches(0.86))
            for cap, frac, fill in (("with AI", 0.94, AMBER),
                                    ("us today", 0.42, RGBColor(0xCB, 0xD5,
                                                                0xE1))):
                by = _e(zy + (Inches(0.30) if frac > 0.5 else Inches(0.64)))
                txt(s, zx, _e(by + Inches(0.015)), Inches(0.82), Inches(0.22),
                    cap, size=8.5, color=MUTED, align=PP_ALIGN.RIGHT)
                shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, bx, by, track,
                      Inches(0.20), fill=MIST, adjust=[0.5])
                shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, bx, by,
                      _e(track * frac), Inches(0.20), fill=fill,
                      adjust=[0.5])

        elif i == 1:
            # the perimeter as it stands: dashed, with things crossing it
            txt(s, zx, zy, zw, Inches(0.22), "OUR PERIMETER, AS IT STANDS",
                size=8, bold=True, color=FAINT)
            fw, fh = Inches(2.78), Inches(0.74)
            fy = _e(zy + Inches(0.22))
            dash_edge(shape(s, MSO_SHAPE.RECTANGLE, zx, fy, fw, fh,
                            fill=RED_T, edge=RED, edge_w=1.25))
            chips = ["ChatGPT", "Copilot", "Cursor", "browser AI"]
            for j, name in enumerate(chips):
                px = _e(zx + Inches(0.10) + (j % 2) * Inches(1.32))
                py = _e(fy + Inches(0.09) + (j // 2) * Inches(0.32))
                sp = shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, px, py,
                           Inches(1.24), Inches(0.26), fill=WHITE,
                           edge=RGBColor(0xE7, 0xB8, 0xB8), adjust=[0.3])
                label(sp, name, size=8, color=RED)
            for k in range(3):
                ay = _e(fy + Inches(0.14) + k * Inches(0.24))
                # they run PAST the card's own wall: that is the point
                arrow(s, _e(zx + fw - Inches(0.04)), ay,
                      _e(x + cw + Inches(0.10)), ay, color=RED, width=1.25)

        else:
            # the lift today: five bars, no two alike
            txt(s, zx, zy, zw, Inches(0.22), "THE LIFT, PER PERSON",
                size=8, bold=True, color=FAINT)
            base = _e(zy + Inches(0.94))
            heights = [0.60, 0.16, 0.38, 0.09, 0.52]
            for j, hh in enumerate(heights):
                bx = _e(zx + Inches(0.28) + j * Inches(0.58))
                shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, bx,
                      _e(base - Inches(hh)), Inches(0.32), Inches(hh),
                      fill=BLUE if hh > 0.3 else RGBColor(0xCB, 0xD5, 0xE1),
                      adjust=[0.25])
            line(s, zx, base, _e(zx + zw), base, color=LINE, width=1.0)

        bh = Inches(text_h(body, (cw - Inches(0.44)) / Inches(1), 9.5))
        txt(s, zx, _e(cy + Inches(1.92)), _e(cw - Inches(0.44)), bh, body,
            size=9.5, color=INK)

    # ── three pressures, one answer ────────────────────────────────────────
    bar_y = Inches(5.42)
    for x in xs:
        arrow(s, _e(x + cw / 2), _e(cy + ch + Inches(0.06)),
              Inches(6.665), _e(bar_y - Inches(0.06)),
              color=RGBColor(0xC2, 0xD3, 0xE6), width=1.5)

    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, L, bar_y, WD, Inches(0.92),
          fill=NAVY, adjust=[0.10])
    shape(s, MSO_SHAPE.RECTANGLE, L, bar_y, Pt(4), Inches(0.92), fill=GREEN)
    txt(s, Inches(0.92), _e(bar_y + Inches(0.20)), Inches(4.20),
        Inches(0.56), "So we build our own — and own the whole path",
        size=13.5, bold=True, color=WHITE)

    chips = ["our model, our hardware", "every call logged",
             "scoped to the role", "the same tools for everyone"]
    cwid, gap = Inches(1.72), Inches(0.15)
    x = Inches(5.34)
    for name in chips:
        sp = shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x,
                   _e(bar_y + Inches(0.24)), cwid, Inches(0.44),
                   fill=RGBColor(0x14, 0x2C, 0x45), edge=GREEN, edge_w=1.0,
                   adjust=[0.22])
        label(sp, name, size=8.5, color=WHITE, bold=False)
        x = _e(x + cwid + gap)

    txt(s, L, Inches(6.56), WD, Inches(0.30),
        "Buying more seats does not answer the control question — it only "
        "spreads it. Owning the platform does.",
        size=10, color=MUTED)

    notes(s, """
The argument, in the order the page makes it.

1. Standing still is now the expensive option. The teams we compete with are
shipping with AI in the loop, and our own engineers already know it — the
demand is not hypothetical, it is here and it is being met somewhere else.
Choosing not to decide is still a decision, and it is the one that costs the
most.

2. It is already in the building, just not through us. People are using
personal accounts, browser extensions and free tiers, and pasting whatever
they are working on into them. We cannot say which tools are in use, what left
with them, what it costs, or what a customer would be told if they asked.
There is no allow-list to enforce and no log to produce. This is the control
problem, and it gets worse every month we leave it alone.

3. The gain is real but uneven. Whoever found a tool got faster; nobody else
did. None of that speed is shared, repeatable or measurable, so we cannot
plan around it or prove it.

The answer: one platform we run ourselves. Our own model on our own hardware,
so the code and the customer data never leave. Every call through one gate, so
there is a log and an allow-list. Tools scoped to a role, with approval before
anything reaches outside. And the same capability for every engineer, not just
the ones who went looking.

The following pages describe what that platform is, how it is contained, and
what it measurably does today.
""")

# ─────────────────────────────────────────────────────── page 2: the hub ───
def page_hub(prs):
    s = page(prs, "AIForgeCrew — a coding agent we run ourselves",
             "Give it a ticket, get a merge request. Our code stays on our "
             "machines and talks to our own model.",
             accent=BLUE,
             stats=[(M["tools"], "things it can do", BLUE),
                    (M["roles"], "agents", BLUE),
                    (M["tests"], "tests", GREEN),
                    ("0", "scan findings", GREEN)])

    # ── the hub ────────────────────────────────────────────────────────────
    cx, cy = Inches(6.24), Inches(3.06)
    r_out = Inches(1.30)
    shape(s, MSO_SHAPE.OVAL, _e(cx - r_out), _e(cy - r_out),
          _e(r_out * 2), _e(r_out * 2), fill=BLUE_T)
    core = shape(s, MSO_SHAPE.OVAL, _e(cx - Inches(1.02)),
                 _e(cy - Inches(1.02)), Inches(2.04), Inches(2.04),
                 gradient=(NAVY_2, BLUE), angle=45.0)
    label(core, "One program\none port · one folder\nno database",
          size=13, color=WHITE)

    feeds = [("A ticket", "Jira · GitLab", Inches(1.86)),
             ("A chat", "ask · plan · delegate", Inches(2.62)),
             ("A schedule", "runs unattended", Inches(3.38))]
    for name, sub, y in feeds:
        w = Inches(2.30)
        sp = shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, L, y, w, Inches(0.56),
                   fill=WHITE, edge=LINE, adjust=[0.18])
        label(sp, f"{name}\n{sub}", size=11, color=INK)
        arrow(s, _e(L + w), _e(y + Inches(0.28)), _e(cx - r_out - Inches(0.06)),
              _e(cy), color=RGBColor(0xC2, 0xD3, 0xE6))

    outs = [("Our own model", "on our hardware", GREEN, Inches(1.86)),
            ("Our tools", f"Jira {M['jira']} · Confluence {M['confluence']} · "
                          f"GitLab {M['gitlab']} · MCP", PLUM, Inches(2.62)),
            ("Our memory", "markdown + one SQLite", TEAL, Inches(3.38))]
    for name, sub, col, y in outs:
        x, w = Inches(9.30), Inches(3.41)
        sp = shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, Inches(0.56),
                   fill=WHITE, edge=col, adjust=[0.18])
        label(sp, f"{name}\n{sub}", size=11, color=col)
        arrow(s, _e(cx + r_out + Inches(0.06)), _e(cy), x,
              _e(y + Inches(0.28)), color=RGBColor(0xC2, 0xD3, 0xE6))

    txt(s, L, Inches(4.16), Inches(3.4), Inches(0.26),
        "WHAT GOES IN", size=9, bold=True, color=FAINT)
    txt(s, Inches(9.30), Inches(4.16), Inches(3.41), Inches(0.26),
        "WHAT IT USES — ALL OURS", size=9, bold=True, color=FAINT,
        align=PP_ALIGN.RIGHT)

    # ── the ticket's journey, as a chevron run ────────────────────────────
    txt(s, L, Inches(4.62), Inches(6.0), Inches(0.26),
        "ONE TICKET, END TO END", size=9, bold=True, color=FAINT)
    steps = [("Understand", BLUE), ("Plan", BLUE), ("Write code", TEAL),
             ("Test and fix", TEAL), ("Check it works", PLUM),
             ("Merge request", GREEN)]
    cw, ch = Inches(2.08), Inches(0.62)
    overlap = Inches(0.10)
    x = L
    for i, (name, col) in enumerate(steps):
        sp = shape(s, MSO_SHAPE.CHEVRON, x, Inches(4.94), cw, ch,
                   fill=col if i == len(steps) - 1 else WHITE,
                   edge=col, edge_w=1.25, adjust=[0.22])
        label(sp, name, size=10.5,
              color=WHITE if i == len(steps) - 1 else col)
        x = _e(x + cw - overlap)

    txt(s, L, Inches(5.68), WD, Inches(0.28),
        "up to four parts of the job run side by side, each in its own copy "
        "of the repo  ·  a person reviews the merge request — nothing merges "
        "itself", size=9.5, color=MUTED)

    # ── the two loops that compound ───────────────────────────────────────
    y = Inches(6.10)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, L, y, WD, Inches(0.86),
          fill=MIST, adjust=[0.14])
    bullet(s, _e(L + Inches(0.22)), _e(y + Inches(0.14)), Inches(5.80),
           "↻", "Writes its own playbook —",
           "plain-text rules, skills and workflows, tidied nightly",
           AMBER, size=9.5)
    bullet(s, Inches(6.62), _e(y + Inches(0.14)), Inches(5.90), "◎",
           "Remembers —", "what one machine learns, the team has tomorrow",
           TEAL, size=9.5)
    txt(s, _e(L + Inches(0.64)), _e(y + Inches(0.48)), Inches(5.2),
        Inches(0.26), "a fix found once becomes a step it repeats",
        size=8.5, color=MUTED)
    txt(s, Inches(7.04), _e(y + Inches(0.48)), Inches(5.4), Inches(0.26),
        "secrets are stripped before anything is shared", size=8.5,
        color=MUTED)

    notes(s, f"""
One FastAPI process, one port, one folder of state ({MEASURED}). Chat engine,
agent pipeline, memory, integrations and job scheduler are libraries inside it.
Python 3.12 and one model endpoint; no GPU, torch, Postgres, Neo4j, vector
store or message broker. Runs on a desktop, a NUC, a server under systemd or a
container — the same single command. Upgrades pull and restart; the data
migrates in place. Backup is copying one folder.

The pipeline in full: Triage, Enhancer, Architect, Planner, Verifier, then a
build loop of up to four parallel subtasks each in its own git worktree (Doer
edits, tests run, Feedback reads failures, Refiner fixes), then Validator
gates, Live-verifier runs the real recipe, Learner writes memory back, and the
result is a merge request a human reviews. Reasoning is plain text, so any
model drives it — no vendor function-calling. Simple mode writes, Plan mode is
read-only, Team mode runs the whole pipeline.

108 tools in the chat catalogue, 19 agent roles, {M['ncloc']} lines of
production code, 8,606 tests, zero findings of any kind from the scanner.
Integrations: Jira 21 tools, Confluence 14, GitLab 10, plus GitHub, e-mail and
any MCP server; a tool with no credentials hides itself. A tree-sitter and
PageRank repo map plus 5 knowledge-graph tools choose context instead of
pasting it. Persistent shell, language server, type-checker, test runner and
an IPython kernel are all real, not simulated. A local vision model can look
at the running app.

Where we go beyond the open-source terminal agents (OpenCode, Aider, Cline):
they stop at one developer's machine and one chat loop, and leave the
enterprise plumbing to you — shared team memory, one egress gate across tool,
shell and notebook, our own certificate authority, and approvals before
anything reaches outside.
""")


# ──────────────────────────────────────────────── page 3: the containment ──
def page_rings(prs):
    s = page(prs, "Nothing gets out unless we allow it",
             "A tool call, a shell command and a notebook cell all reach the "
             "same gate and get the same answer.",
             accent=RED,
             stats=[("0", "known weaknesses", GREEN),
                    ("0", "usage data sent out", GREEN),
                    ("20", "actions that ask first", AMBER)])

    # ── concentric containment ────────────────────────────────────────────
    cx, cy = Inches(5.02), Inches(4.06)
    rings = [
        (Inches(2.10), RGBColor(0xEF, 0xF2, 0xF6), FAINT,
         "the firewall on the machine — yours, not ours"),
        (Inches(1.62), RED_T, RED, "ONE GATE  ·  tool · shell · notebook"),
        (Inches(1.14), AMBER_T, AMBER, "workspace + approvals"),
    ]
    for r, fill, edge, _cap in rings:
        shape(s, MSO_SHAPE.OVAL, _e(cx - r), _e(cy - r), _e(r * 2), _e(r * 2),
              fill=fill, edge=edge, edge_w=1.25)
    core = shape(s, MSO_SHAPE.OVAL, _e(cx - Inches(0.68)),
                 _e(cy - Inches(0.68)), Inches(1.36), Inches(1.36),
                 gradient=(NAVY_2, TEAL), angle=45.0)
    label(core, "Our code\nand our keys", size=12, color=WHITE)

    # ring captions, sitting ON their ring
    for r, _f, col, cap in rings:
        cap_w = Inches(3.0)
        cap_h = Inches(text_h(cap, 2.86, 8.5))
        shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, _e(cx - cap_w / 2),
              _e(cy - r - cap_h / 2), cap_w, cap_h, fill=WHITE, edge=col,
              adjust=[0.4])
        txt(s, _e(cx - cap_w / 2), _e(cy - r - cap_h / 2 + Inches(0.02)),
            cap_w, cap_h, cap, size=8.5, bold=True, color=col,
            align=PP_ALIGN.CENTER)

    # the three transports, arriving from the left
    for i, (name, sub) in enumerate([("A tool call", "web · Jira · MCP"),
                                     ("A shell command", "curl · scp · ssh"),
                                     ("A notebook cell", "its own code")]):
        y = Inches(3.06) + i * Inches(0.58)
        txt(s, L, y, Inches(1.86), Inches(0.24), name, size=10, bold=True,
            color=INK, align=PP_ALIGN.RIGHT)
        txt(s, L, _e(y + Inches(0.21)), Inches(1.86), Inches(0.22), sub,
            size=8, color=MUTED, align=PP_ALIGN.RIGHT)
        arrow(s, Inches(2.60), _e(y + Inches(0.16)), Inches(2.90),
              _e(cy - Inches(0.26) + i * Inches(0.26)), color=RED)

    txt(s, L, Inches(4.94), Inches(2.06), Inches(0.66),
        "the gate is code, not the model's judgement — a prompt cannot "
        "argue its way past it", size=8.5, italic=True, color=MUTED)

    # ── allowed / refused, as two solid blocks ────────────────────────────
    x = Inches(7.30)
    w = Inches(5.40)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(2.06), w, Inches(1.46),
          fill=GREEN_T, adjust=[0.10])
    txt(s, _e(x + Inches(0.20)), Inches(2.18), Inches(2.0), Inches(0.28),
        "✓  ALLOWED", size=11, bold=True, color=GREEN)
    for i, item in enumerate(["our own network and machines",
                              "pushing to our own git remote",
                              "deploying to a server we named"]):
        txt(s, _e(x + Inches(0.20)), _e(Inches(2.50) + i * Inches(0.30)),
            _e(w - Inches(0.40)), Inches(0.26), f"·  {item}", size=9.5)

    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(3.66), w, Inches(1.76),
          fill=RED_T, adjust=[0.10])
    txt(s, _e(x + Inches(0.20)), Inches(3.78), Inches(2.0), Inches(0.28),
        "✕  REFUSED", size=11, bold=True, color=RED)
    for i, item in enumerate(["copying files to an outside host",
                              "uploading to cloud storage",
                              "web search — removed completely",
                              "anything not on the list"]):
        txt(s, _e(x + Inches(0.20)), _e(Inches(4.10) + i * Inches(0.30)),
            _e(w - Inches(0.40)), Inches(0.26), f"·  {item}", size=9.5)

    # ── the three facts under it ──────────────────────────────────────────
    y = Inches(5.62)
    shape(s, MSO_SHAPE.RECTANGLE, x, y, w, Pt(1), fill=LINE)
    facts = [("⚿", "Keys", "one locked folder, never shown back", AMBER),
             ("✔", "Certificates", "our own CA, added in Settings, no restart",
              BLUE),
             ("≡", "Record", "every step of every run is written down", TEAL)]
    for i, (glyph, head, body, col) in enumerate(facts):
        bullet(s, _e(x + Inches(0.02)), _e(y + Inches(0.22) + i * Inches(0.40)),
               _e(w - Inches(0.04)), glyph, f"{head} —", body, col, size=9.5)

    # ── what we do not claim ──────────────────────────────────────────────
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, L, Inches(6.60), Inches(6.40),
          Inches(0.52), fill=MIST, adjust=[0.30])
    txt(s, _e(L + Inches(0.18)), Inches(6.72), Inches(6.04), Inches(0.30),
        "We do not claim a sealed box: a shell can still open a socket the "
        "gate never sees.", size=9, color=MUTED)

    notes(s, f"""
The incident that set the bar: a fetch the tool refused was rerouted through a
shell curl, then through a notebook cell, and it worked. A boundary you can
walk around by changing transport is a suggestion, so one module —
net/egress.py — answers on all three paths and after every redirect, and a
missing allow-list entry is DENIED rather than escalated to a human. Every
control here began as a bypass that WORKED in testing, and each is pinned by a
test verified to fail on the tree before the fix ({MEASURED}).

Refused outbound: scp, rsync, sftp, ssh host 'cmd', curl -d/-F/-T, aws s3 cp,
docker push, npm publish, git push to a URL, and bash's own
> /dev/tcp/host/port. Inbound: web fetch, crawl, headless browser, @mention
expansion. Web SEARCH was deleted outright because the query string is our own
data leaving the box. AIFORGE_EGRESS_OFF closes everything in one switch;
per-class switches cover integrations, e-mail, telemetry, MCP and fleet sync.
Loopback, LAN, a local registry and a push to a named remote stay allowed on
purpose — a control that blocks ordinary work gets switched off.

Access: a token is required on every API route but health; a non-loopback bind
with NO token refuses to boot. Loopback trust must be declared, because behind
a same-host reverse proxy every request looks like 127.0.0.1 — which was a
full auth bypass. CORS is an allow-list, never a wildcard. Limits: one shared
token, so no per-user identity or SSO yet.

Untrusted input: a ticket description or a fetched page reaches the model
beside our own instruction and we do not classify it. So the answer is
containment: 20 external-write tools ask a human, file tools are clamped to
the workspace, Plan mode cannot write, and unattended runs refuse writes.

Certificates: one bundle covers the model endpoint, Jira, Confluence, GitLab,
our own HTTP and every subprocess (git, curl, npm). Roots and intermediates
are added in Settings, listed with what each one is, and take hold with no
restart. No CERT_NONE anywhere in the tree.

Stated limits, printed in the product's own docs: the notebook guard is a
guard and not a sandbox; a shell can open a socket the policy never sees;
fleet sync is unauthenticated by default; role tool filters fail OPEN so a
typo cannot brick an agent.
""")


# ────────────────────────────────────────────────── page 4: how it grows ───
def page_growth(prs):
    s = page(prs, "What one person learns, the whole company keeps",
             "Each machine learns on its own. Secrets are stripped, a team "
             "lead merges the rest, and other teams read it.",
             accent=TEAL,
             stats=[(M["tests"], "tests", GREEN),
                    (M["coverage"], "covered", BLUE),
                    ("A·A·A", "ratings", GREEN)])

    # ── widening circles: machine → team → company ────────────────────────
    cx, cy = Inches(3.60), Inches(4.06)
    for r, fill, edge in [(Inches(2.28), RGBColor(0xEE, 0xF6, 0xF6), TEAL),
                          (Inches(1.62), RGBColor(0xE2, 0xF0, 0xF0), TEAL),
                          (Inches(0.94), WHITE, TEAL)]:
        shape(s, MSO_SHAPE.OVAL, _e(cx - r), _e(cy - r), _e(r * 2), _e(r * 2),
              fill=fill, edge=edge, edge_w=1.25)
    txt(s, _e(cx - Inches(0.86)), _e(cy - Inches(0.30)), Inches(1.72),
        Inches(0.60), "One machine\nlearns from every run", size=10.5,
        bold=True, color=INK, align=PP_ALIGN.CENTER)
    txt(s, _e(cx - Inches(1.50)), _e(cy - Inches(1.48)), Inches(3.0),
        Inches(0.30), "OUR TEAM", size=9.5, bold=True, color=TEAL,
        align=PP_ALIGN.CENTER)
    txt(s, _e(cx - Inches(1.90)), _e(cy - Inches(2.16)), Inches(3.8),
        Inches(0.30), "THE COMPANY", size=9.5, bold=True, color=TEAL,
        align=PP_ALIGN.CENTER)

    # Machines ON the rings: the claim is "many of these", and a picture of
    # one circle labelled "the company" does not carry it.
    import math
    for r_in, count, d_in, col in ((1.62, 5, 0.20, TEAL),
                                   (2.28, 7, 0.15, RGBColor(0x7C, 0xA9, 0xA9))):
        for k in range(count):
            a = math.radians(-58 + k * (116 / max(count - 1, 1)))
            px = cx + Inches(r_in * math.cos(a))
            py = cy + Inches(r_in * math.sin(a))
            d = Inches(d_in)
            shape(s, MSO_SHAPE.OVAL, _e(px - d / 2), _e(py - d / 2), d, d,
                  fill=WHITE, edge=col, edge_w=1.0)

    # the redaction gate sits ON the first ring it guards
    gw, gh = Inches(2.26), Inches(0.46)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, _e(cx - gw / 2),
          _e(cy + Inches(0.94) - gh / 2), gw, gh, fill=AMBER_T, edge=AMBER,
          adjust=[0.35])
    txt(s, _e(cx - gw / 2), _e(cy + Inches(0.94) - gh / 2 + Inches(0.06)),
        gw, Inches(0.34), "secrets stripped here", size=9, bold=True,
        color=AMBER, align=PP_ALIGN.CENTER)

    txt(s, L, Inches(6.44), Inches(5.6), Inches(0.44),
        "only distilled notes travel — transcripts and raw captures never "
        "leave the machine that made them", size=9, italic=True, color=MUTED)

    # ── what each audience gets, as bullet lines ──────────────────────────
    x, w = Inches(6.60), Inches(6.12)
    rows = [
        ("◆", "Our team", [
            ("a fix found once", "is known by everyone tomorrow"),
            ("a new joiner", "reads the team's real history"),
            ("people leave", "and their know-how stays"),
        ], GREEN),
        ("■", "The company", [
            ("one place", "serves many teams at once"),
            ("each team's memory", "stays its own"),
            ("nothing to run", "— it is files in a folder"),
        ], BLUE),
        ("●", "Legal and audit", [
            ("passwords", "are blocked before sharing, not after"),
            ("every merge", "is saved and can be undone"),
            ("delete the folder", "and it is gone — no second copy"),
        ], PLUM),
    ]
    y = Inches(1.94)
    for glyph, head, items, col in rows:
        shape(s, MSO_SHAPE.RECTANGLE, x, y, Inches(0.05), Inches(1.28),
              fill=col)
        txt(s, _e(x + Inches(0.20)), y, Inches(3.0), Inches(0.28), head,
            size=12, bold=True, color=col)
        for i, (lead, rest) in enumerate(items):
            bullet(s, _e(x + Inches(0.20)),
                   _e(y + Inches(0.34) + i * Inches(0.31)),
                   _e(w - Inches(0.20)), glyph, lead, rest, col, size=9.5)
        y = _e(y + Inches(1.36))

    # ── how it lands ──────────────────────────────────────────────────────
    y = Inches(6.28)
    txt(s, x, _e(y - Inches(0.30)), Inches(4.0), Inches(0.24),
        "HOW WE WOULD ROLL IT OUT", size=9, bold=True, color=FAINT)
    line(s, _e(x + Inches(0.12)), _e(y + Inches(0.16)),
         _e(x + w - Inches(0.30)), _e(y + Inches(0.16)), color=LINE, width=2)
    steps = [("one team", "on real tickets"),
             ("a second team", "same admin, memory already there"),
             ("company-wide", "after a month of two")]
    step_w = (w - Inches(0.30)) / len(steps)
    for i, (head, sub) in enumerate(steps):
        sx = _e(x + Inches(0.12) + i * step_w)
        shape(s, MSO_SHAPE.OVAL, sx, _e(y + Inches(0.04)), Inches(0.24),
              Inches(0.24), fill=TEAL)
        txt(s, _e(sx + Inches(0.32)), _e(y + Inches(0.00)),
            _e(step_w - Inches(0.34)), Inches(0.24), head, size=10, bold=True,
            color=INK)
        txt(s, _e(sx + Inches(0.32)), _e(y + Inches(0.21)),
            _e(step_w - Inches(0.34)), Inches(0.30), sub, size=8.5,
            color=MUTED)

    notes(s, f"""
Memory in detail ({MEASURED}). Each machine compacts what it learned locally. A
redaction filter runs on the CLIENT before anything is advertised:
credential-shaped and private notes are BLOCKED, never quietly edited. One team
admin is the only merger, folding into per-group trees so two teams cannot mix,
and one admin can serve many independent fleets — groups are discovered from
the admin rather than hardcoded. Only distilled knowledge nodes travel;
transcripts, captures and working notes never leave the machine that made them.
Every merge is snapshotted and a revert endpoint restores it. Sync is
spoke-initiated, so laptops behind NAT just work, and moving a machine is
copying a directory. Deploy note: the admin holds the merged fold — bind it to
the LAN or a WireGuard network, because the sync surface is open by default.

The quality numbers are the PLATFORM's, read off the scanner API rather than
inferred from a green pipeline: 0 bugs, 0 vulnerabilities, 0 code smells,
A/A/A ratings, 8,606 tests, 82.8% coverage as Sonar scopes it (86.7% by
pytest-cov on the same run), 71,204 lines, 542 files, 0.1% duplication. Three
test failures are named rather than rounded away: all three need a model
actually served on the shared endpoint, which was empty. Nine standing
vulnerabilities were closed by changing code — TLS pinning, scheme validation,
digest usage — after two earlier passes had triaged them as accepted decisions.

What that number does NOT say: it is the platform's quality, not the agent's
output. There is no evaluation harness — no frozen set of closed tickets
replayed each release, no acceptance rate, no edit distance between what the
agent wrote and what shipped, and nothing re-run when the model is swapped.
There is no load test, so no honest number for users per box. Backup is copying
one folder, but there is no documented restore drill, and the traces name the
BOX rather than a person.

What it asks of us: one machine with a model on it, one folder to back up, and
upgrades that pull, restart and migrate themselves. Decisions we still need:
fund the scoring harness; name an owner and a stand-in; and pick the network
that holds the company memory.
""")


def _normalise(path: Path) -> list[str]:
    """Rewrite the saved package into what PowerPoint expects to read FIRST.

    "The file format is invalid" is the message PowerPoint gives before it has
    looked at a single slide, so the cause is in the package header, and two
    things there come from python-pptx's default template rather than from us:

    * ``<p:sldSz>`` keeps the template's ``type="screen4x3"`` attribute while
      we set a 16:9 size, so the declared aspect ratio contradicts the
      dimensions. A deck PowerPoint had accepted carries no ``type`` at all;
    * the width lands on 12191695 EMU because 13.333in is not exactly 16:9.
      Every Office-written 16:9 deck says 12192000 exactly.

    Also drops the printerSettings part the template drags in — a binary blob
    describing someone else's printer that nothing in this deck references.

    Returns a list of what it changed, so the build says it out loud.
    """
    import re
    import shutil
    import zipfile

    changed, tmp = [], path.with_suffix(".tmp.pptx")
    with zipfile.ZipFile(path) as src, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        names = [n for n in src.namelist()
                 if "printerSettings" not in n]
        if len(names) != len(src.namelist()):
            changed.append("dropped the template's printerSettings part")
        for name in names:
            data = src.read(name)
            if name == "ppt/presentation.xml":
                xml = data.decode()
                fixed = re.sub(r'<p:sldSz[^/]*/>',
                               '<p:sldSz cx="12192000" cy="6858000"/>', xml)
                if fixed != xml:
                    changed.append('sldSz → 12192000x6858000, no "type"')
                data = fixed.encode()
            elif name == "[Content_Types].xml":
                xml = data.decode()
                fixed = re.sub(r'<Default Extension="bin"[^>]*/>', "", xml)
                fixed = re.sub(
                    r'<Override PartName="[^"]*printerSettings[^"]*"[^>]*/>',
                    "", fixed)
                if fixed != xml:
                    changed.append("removed the printerSettings content type")
                data = fixed.encode()
            elif name.endswith(".rels"):
                xml = data.decode()
                fixed = re.sub(r'<Relationship[^>]*printerSettings[^>]*/>',
                               "", xml)
                if fixed != xml:
                    changed.append(f"removed a printerSettings link in {name}")
                data = fixed.encode()
            if name.endswith((".xml", ".rels")):
                # lxml writes <?xml version='1.0' ... ?> with single quotes.
                # Valid XML, and the only remaining difference from a package
                # Office itself wrote — so it goes too rather than being left
                # as the one thing still unexplained.
                data = data.replace(
                    b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>",
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                    1)
            dst.writestr(name, data)
    shutil.move(str(tmp), str(path))
    return changed


def _rewrite_with_libreoffice(path: Path) -> bool:
    """Re-save the deck through LibreOffice, and ship THAT file.

    python-pptx writes a package that passes every structural check there is —
    zip integrity, content types, relationships, slide ids, element order, no
    float EMU — and LibreOffice renders it perfectly. It still would not open
    in the reviewer's PowerPoint. When a file is provably valid and one reader
    still refuses it, arguing with the reader is not a plan: re-writing the
    same slides through a second, independent implementation is.

    Best effort. No LibreOffice on the machine (CI, a laptop) simply leaves
    the python-pptx output in place, which is what the checks say is fine.
    """
    import shutil
    import subprocess
    import tempfile

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to",
                 "pptx:Impress MS PowerPoint 2007 XML", "--outdir", tmp,
                 str(path)],
                check=True, capture_output=True, timeout=180)
        except (subprocess.SubprocessError, OSError):
            return False
        out = Path(tmp) / path.name
        if not out.is_file() or out.stat().st_size < 20_000:
            return False        # a truncated conversion is worse than none
        shutil.copyfile(out, path)
    return True


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    page_why(prs)
    page_hub(prs)
    page_rings(prs)
    page_growth(prs)
    prs.save(OUT)
    for note in _normalise(OUT):
        print(f"  normalised: {note}")
    if _rewrite_with_libreoffice(OUT):
        print("  rewritten by LibreOffice (a second, independent writer)")
        for note in _normalise(OUT):
            print(f"  normalised: {note}")
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB, "
          f"{len(prs.slides._sldIdLst)} slides)")


if __name__ == "__main__":
    build()
