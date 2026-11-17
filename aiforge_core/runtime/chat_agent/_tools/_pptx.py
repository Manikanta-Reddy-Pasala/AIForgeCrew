"""PowerPoint tools: read, create and edit ``.pptx`` decks.

``python-pptx`` is an OPTIONAL extra (``pip install 'aiforgecrew[pptx]'``). It
is imported inside each call, never at module load, so a host without it
starts and runs every other tool as before; these three return
``{"ok": False, "error": "python-pptx is not installed: ..."}`` instead.

Slide numbers are 1-based everywhere (``slide: 1`` is the first slide), the
way a person names them. Positions and sizes in ops are inches.

A new deck gets one of the built-in looks (``style``, see ``_STYLES``):
``plain`` (the default: white, Calibri, dark text, one accent bar under each
title), ``classic``, ``business`` or ``modern``. The style and accent are
remembered in the deck's core-properties keywords, so ``pptx_edit``'s
``add_slide`` draws new slides the same way. With ``template`` (a .pptx path
or the name of a template saved with ``pptx_save_template``) the template's
layouts and theme are used and no styling is forced, unless the template
remembers a style or ``style`` is given.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path

MISSING = "python-pptx is not installed: pip install 'aiforgecrew[pptx]'"

#: The built-in looks. Every one is 16:9 with titles >= 36 pt, level-0
#: bullets >= 24 pt and tables >= 16 pt. The pptx_create ``accent`` arg
#: replaces ``accent``; the other colours stay. ``title`` None = whichever of
#: white/near-black reads best on the accent (a title drawn on an accent band).
_STYLES: dict[str, dict] = {
    "plain": {
        "desc": "white, Calibri, dark bold titles, a short accent bar under each title",
        "bg": "FFFFFF", "title_font": "Calibri", "body_font": "Calibri",
        "title": "1F1F1F", "body": "333333", "muted": "595959",
        "accent": "1F4E79", "title_pt": 36, "title_bold": True,
        "body_pt": (26, 22, 20, 18), "table_pt": 18,
        "cover_pt": 44, "subtitle_pt": 24,
        "row_a": "FFFFFF", "row_b": "FFFFFF", "row_text": "333333",
    },
    "classic": {
        "desc": "warm off-white, Georgia serif, navy titles over a thin full-width rule",
        "bg": "FAF7F0", "title_font": "Georgia", "body_font": "Georgia",
        "title": "1B2A4A", "body": "2B2B2B", "muted": "6B6B6B",
        "accent": "1B2A4A", "title_pt": 40, "title_bold": False,
        "body_pt": (26, 22, 20, 18), "table_pt": 18,
        "cover_pt": 50, "subtitle_pt": 26,
        "row_a": "FFFFFF", "row_b": "EFEBE2", "row_text": "2B2B2B",
    },
    "business": {
        "desc": "white, a dark-blue title band across the top, Calibri",
        "bg": "FFFFFF", "title_font": "Calibri", "body_font": "Calibri",
        "title": None, "body": "262626", "muted": "595959",
        "accent": "1F3864", "stripe": "4A90D9", "title_pt": 36, "title_bold": True,
        "body_pt": (26, 22, 20, 18), "table_pt": 18,
        "cover_pt": 48, "subtitle_pt": 24,
        "row_a": "FFFFFF", "row_b": "E9EFF7", "row_text": "262626",
    },
    "modern": {
        "desc": "dark charcoal, light Arial text, one bright teal accent",
        "bg": "1E1F26", "title_font": "Arial", "body_font": "Arial",
        "title": "FFFFFF", "body": "E4E6EB", "muted": "A9B0BC",
        "accent": "14B8A6", "title_pt": 40, "title_bold": True,
        "body_pt": (26, 22, 20, 18), "table_pt": 18,
        "cover_pt": 56, "subtitle_pt": 26,
        "row_a": "2A2C35", "row_b": "343744", "row_text": "E4E6EB", "grid": "1E1F26",
    },
}
for _k, _v in _STYLES.items():
    _v["name"] = _k
_PLAIN = _STYLES["plain"]
_DARK = _PLAIN["title"]
_BODY = _PLAIN["body"]
_WHITE = "FFFFFF"
_ACCENT = _PLAIN["accent"]
_FONT = _PLAIN["body_font"]
_TITLE_PT = _PLAIN["title_pt"]
_BODY_PT = _PLAIN["body_pt"]
_TABLE_PT = _PLAIN["table_pt"]
#: Title-slide sizes: the big title and the line under it.
_COVER_PT, _SUBTITLE_PT = _PLAIN["cover_pt"], _PLAIN["subtitle_pt"]
_MUTED = _PLAIN["muted"]
_ACCENT_BAR = "Accent bar"
#: Where a deck remembers its look: tokens in the core-properties keywords.
_META_STYLE, _META_ACCENT = "aiforge-style:", "aiforge-accent:"
_TPL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_READ_BUDGET = 60_000
_EMU_IN = 914400


class _OpError(Exception):
    """One edit op could not be applied (nothing is saved)."""


def _lib():
    """The python-pptx entry point, or None when the extra is not installed."""
    try:
        from pptx import Presentation
    except Exception:  # noqa: BLE001 — missing extra / broken install
        return None
    return Presentation


def _missing() -> dict:
    return {"ok": False, "error": MISSING}


# ── paths ────────────────────────────────────────────────────────────────
def _resolve(cwd: str, raw: str) -> Path:
    from .._shell import _resolve as shell_resolve
    return shell_resolve(cwd, raw)


def _resolve_existing(cwd: str, raw: str) -> Path | None:
    """An existing file: the path itself, else an attachment of that name in
    the session's media folder. Workspace rules apply to both."""
    p = _resolve(cwd, raw)
    if p.is_file():
        return p
    alt = _resolve(cwd, os.path.join(".aiforge", "media", os.path.basename(raw)))
    return alt if alt.is_file() else None


def _arg_path(args: dict, *keys: str) -> str:
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _json_list(value, name: str) -> list:
    """A list argument; a model sometimes sends it as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise _OpError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise _OpError(f"{name} must be a list")
    return value


def _save(prs, dest: Path) -> None:
    """Write via a temp file in the same folder, then rename over ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        prs.save(str(tmp))
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


# ── small helpers ────────────────────────────────────────────────────────
def _rgb(hexstr: str):
    from pptx.dml.color import RGBColor
    s = str(hexstr or "").strip().lstrip("#")
    if len(s) != 6:
        raise _OpError(f"colour must be 6 hex digits, got {hexstr!r}")
    try:
        return RGBColor.from_string(s.upper())
    except ValueError as exc:
        raise _OpError(f"colour must be 6 hex digits, got {hexstr!r}") from exc


def _inches(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v) * _EMU_IN)
    except (TypeError, ValueError) as exc:
        raise _OpError(f"not a number of inches: {v!r}") from exc


def _iter_shapes(shapes):
    """Every shape, descending into groups."""
    for sh in shapes:
        yield sh
        if sh._element.tag.endswith("}grpSp"):
            yield from _iter_shapes(sh.shapes)


def _ph_type(sh) -> str:
    if not getattr(sh, "is_placeholder", False):
        return ""
    t = sh.placeholder_format.type
    return str(getattr(t, "name", t)).lower()


def _flatten(items, level: int = 0) -> list[tuple[str, int]]:
    """Bullets as ``(text, level)``. A string is one bullet (each leading tab
    or two spaces is one level deeper); ``{"text", "level"?, "children"?}``
    sets the level; a nested list is one level below the item before it."""
    out: list[tuple[str, int]] = []
    for it in items or []:
        if isinstance(it, (list, tuple)):
            out += _flatten(it, level + 1)
        elif isinstance(it, dict):
            lv = it.get("level")
            try:
                lv = level if lv is None else int(lv)
            except (TypeError, ValueError):
                lv = level
            lv = max(0, min(lv, 8))
            out.append((str(it.get("text") or ""), lv))
            kids = it.get("children") or it.get("bullets")
            if kids:
                out += _flatten(kids, lv + 1)
        elif it is not None:
            s = str(it)
            body = s.lstrip(" \t")
            lead = s[:len(s) - len(body)]
            lv = level + lead.count("\t") + lead.count(" ") // 2
            for mark in ("- ", "* ", "• "):
                if body.startswith(mark):
                    body = body[len(mark):]
                    break
            out.append((body.strip(), max(0, min(lv, 8))))
    return out


def _style_run(run, *, size: int, color: str, bold: bool = False,
               font: str = _FONT, italic: bool | None = None) -> None:
    f = run.font
    f.name = font
    f.size = _pt(size)
    f.bold = bold
    if italic is not None:
        f.italic = italic
    f.color.rgb = _rgb(color)


def _lum(hexstr: str) -> float:
    """WCAG relative luminance of a 6-digit hex colour."""
    s = str(hexstr).lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(s[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _on(fill: str) -> str:
    """White or near-black: whichever has more contrast on ``fill``."""
    lum = _lum(fill)
    return _WHITE if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / 0.0627) else "1A1A1A"


def _mix(a: str, b: str, t: float) -> str:
    """``a`` moved ``t`` (0..1) of the way to ``b``."""
    a, b = a.lstrip("#"), b.lstrip("#")
    return "".join(f"{round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t):02X}"
                   for i in (0, 2, 4))


def _title_color(st: dict, accent: str) -> str:
    return st["title"] or _on(accent)


# ── remembered style ─────────────────────────────────────────────────────
def _get_meta(prs) -> dict:
    """``{"style", "accent"}`` the deck remembers (empty when none)."""
    out: dict = {}
    try:
        kw = prs.core_properties.keywords or ""
    except Exception:  # noqa: BLE001 — a deck without core properties
        return out
    for tok in re.split(r"[;,]", kw):
        tok = tok.strip()
        if tok.startswith(_META_STYLE):
            out["style"] = tok[len(_META_STYLE):].strip().lower()
        elif tok.startswith(_META_ACCENT):
            acc = tok[len(_META_ACCENT):].strip().lstrip("#")
            if re.fullmatch(r"[0-9A-Fa-f]{6}", acc):
                out["accent"] = acc.upper()
    return out


def _set_meta(prs, style: str, accent: str) -> None:
    kw = prs.core_properties.keywords or ""
    keep = [t.strip() for t in re.split(r"[;,]", kw)
            if t.strip() and not t.strip().startswith((_META_STYLE, _META_ACCENT))]
    prs.core_properties.keywords = "; ".join(
        keep + [f"{_META_STYLE}{style}", f"{_META_ACCENT}{accent.upper()}"])


def _unknown_style(name: str) -> str:
    return f"unknown style {name}; one of: " + ", ".join(_STYLES)


def _pt(n):
    from pptx.util import Pt
    return Pt(n)


# ── geometry ─────────────────────────────────────────────────────────────
def _geometry(prs, st: dict | None = None) -> dict:
    """Title, accent-bar and content boxes for the slide size (EMU), for the
    style's title treatment (plain/template: a short bar under the title)."""
    w, h = int(prs.slide_width), int(prs.slide_height)
    mx = int(w * 0.045)
    title_top, title_h = int(h * 0.05), int(h * 0.135)
    body_top = int(h * 0.235)
    geo = {
        "title": (mx, title_top, w - 2 * mx, title_h),
        "bar": (mx, title_top + title_h + int(h * 0.006), int(w * 0.09),
                max(int(h * 0.008), 1)),
        "body": (mx, body_top, w - 2 * mx, h - body_top - int(h * 0.07)),
    }
    name = (st or {}).get("name")
    if name == "classic":
        # a thin rule the full content width under the title
        geo["bar"] = (mx, title_top + title_h + int(h * 0.006), w - 2 * mx,
                      max(int(h * 0.0035), 1))
    elif name == "business":
        band_h = int(h * 0.165)
        geo["band"] = (0, 0, w, band_h)
        geo["stripe"] = (0, band_h, w, max(int(h * 0.008), 1))
        geo["title"] = (mx, int(h * 0.015), w - 2 * mx, band_h - int(h * 0.03))
        geo.pop("bar")
    elif name == "modern":
        sw = max(int(w * 0.009), 1)
        geo["bar"] = (mx, title_top + int(title_h * 0.2), sw, int(title_h * 0.6))
        shift = sw + int(w * 0.016)
        geo["title"] = (mx + shift, title_top, w - 2 * mx - shift, title_h)
    return geo


def _split(box, parts: int, *, horizontal: bool):
    x, y, w, h = box
    gap = int((w if horizontal else h) * 0.04)
    if horizontal:
        each = (w - gap * (parts - 1)) // parts
        return [(x + i * (each + gap), y, each, h) for i in range(parts)]
    each = (h - gap * (parts - 1)) // parts
    return [(x, y + i * (each + gap), w, each) for i in range(parts)]


def _place(sh, box) -> None:
    sh.left, sh.top, sh.width, sh.height = (int(v) for v in box)


def _needs_fit(prs) -> bool:
    """True when the layouts' placeholders cover well under the slide width:
    a 4:3 master in a 16:9 deck, as a plain deck made here has. Slides added
    to such a deck get the plain geometry and fonts; a real template's
    layouts are used as they are."""
    w = int(prs.slide_width)
    for lo in prs.slide_layouts:
        for ph in lo.placeholders:
            if ph.placeholder_format.idx == 0 and ph.width:
                return int(ph.left or 0) + int(ph.width) < w * 0.8
    return False


def _deck_accent(prs) -> str:
    acc = _get_meta(prs).get("accent")
    if acc:
        return acc
    for slide in prs.slides:
        for sh in slide.shapes:
            if sh.name == _ACCENT_BAR:
                try:
                    return str(sh.fill.fore_color.rgb)
                except Exception:  # noqa: BLE001
                    break
    return _ACCENT


# ── layouts ──────────────────────────────────────────────────────────────
_SKIP_PH = ("date", "footer", "slide_number")


def _layout_kinds(layout) -> tuple[bool, bool, bool]:
    """(has title, has body, has other content) for a layout."""
    title = body = other = False
    for ph in layout.placeholders:
        t = _ph_type(ph)
        if t == "title":
            title = True
        elif t in ("body", "object") and ph.placeholder_format.idx != 0:
            body = True
        elif t and t not in _SKIP_PH:
            other = True
    return title, body, other


def _pick_layout(prs, *, with_body: bool, name: str = ""):
    layouts = list(prs.slide_layouts)
    if name:
        for lo in layouts:
            if lo.name.strip().lower() == name.strip().lower():
                return lo
        raise _OpError(f"no layout named {name!r}; the deck has: "
                       + ", ".join(lo.name for lo in layouts))
    content = title_only = None
    for lo in layouts:
        title, body, other = _layout_kinds(lo)
        if title and body and content is None:
            content = lo
        if title and not body and not other and title_only is None:
            title_only = lo
    pick = (content or title_only) if with_body else (title_only or content)
    return pick or layouts[0]


# ── writing text ─────────────────────────────────────────────────────────
def _capture_styles(tf) -> dict:
    """Per-level font (name, size, bold, rgb) of the first run at each level,
    so rewritten bullets keep the look they had."""
    styles: dict = {}
    for para in tf.paragraphs:
        if para.level in styles or not para.runs:
            continue
        f = para.runs[0].font
        try:
            rgb = f.color.rgb if f.color and f.color.type is not None else None
        except Exception:  # noqa: BLE001 — theme colour: no rgb
            rgb = None
        styles[para.level] = (f.name, f.size, f.bold, rgb)
    return styles


def _write_bullets(tf, items: list[tuple[str, int]], *, st: dict | None,
                   styles: dict | None = None) -> None:
    """Bullets in the style's body font/sizes (``st`` None: a template's own
    look, keeping the per-level ``styles`` the text had)."""
    tf.clear()
    tf.word_wrap = True
    if st:
        # the default master shrinks text on overflow: sub-points set to 22 pt
        # rendered at ~60%; a built-in style shows the sizes it sets
        from pptx.enum.text import MSO_AUTO_SIZE
        tf.auto_size = MSO_AUTO_SIZE.NONE
    for i, (text, level) in enumerate(items):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.level = level
        run = para.add_run()
        run.text = text
        if st:
            sizes = st["body_pt"]
            _style_run(run, size=sizes[min(level, len(sizes) - 1)],
                       color=st["body"], font=st["body_font"])
        elif styles:
            st = styles.get(level) or styles.get(min(styles, key=lambda k: abs(k - level)))
            name, size, bold, rgb = st
            if name:
                run.font.name = name
            if size:
                run.font.size = size
            if bold is not None:
                run.font.bold = bold
            if rgb is not None:
                run.font.color.rgb = rgb


def _write_title(sh, text: str, *, st: dict | None, accent: str = _ACCENT) -> None:
    tf = sh.text_frame
    if st or not tf.paragraphs[0].runs:
        tf.clear()
        run = tf.paragraphs[0].add_run()
        run.text = text
        if st:
            _style_run(run, size=st["title_pt"], color=_title_color(st, accent),
                       bold=st["title_bold"], font=st["title_font"])
            from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
            tf.paragraphs[0].alignment = PP_ALIGN.LEFT
            if st["name"] != "plain":
                tf.word_wrap = True
                tf.vertical_anchor = (MSO_ANCHOR.BOTTOM if st["name"] == "classic"
                                      else MSO_ANCHOR.MIDDLE)
        return
    para = tf.paragraphs[0]
    para.runs[0].text = text
    for r in para.runs[1:]:
        r.text = ""
    for extra in tf.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)


def _title_shape(slide, *, create: bool, st: dict | None, geo: dict):
    sh = slide.shapes.title
    if sh is not None or not create:
        return sh
    for ph in slide.slide_layout.placeholders:
        if ph.placeholder_format.idx == 0:
            sh = slide.shapes.clone_placeholder(ph)
            break
    if sh is None:
        sh = slide.shapes.add_textbox(*geo["title"])
        sh.name = "Title"
        return sh
    sh = slide.shapes.title
    if st:
        _place(sh, geo["title"])
    return sh


def _rect(slide, box, color: str, *, name: str = "", back: bool = False,
          flat: bool = True):
    """A filled rectangle with no outline; ``back`` puts it behind the rest
    (a band or block the title text sits on)."""
    from pptx.enum.shapes import MSO_SHAPE
    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *(int(v) for v in box))
    if name:
        sh.name = name
    sh.fill.solid()
    sh.fill.fore_color.rgb = _rgb(color)
    sh.line.fill.background()
    if flat:
        sh.shadow.inherit = False
    if back:
        tree = slide.shapes._spTree
        tree.remove(sh._element)
        tree.insert(2, sh._element)   # after nvGrpSpPr + grpSpPr
    return sh


def _text_box(slide, box, text: str, *, font: str, size: int, color: str,
              align=None, italic: bool | None = None, bold: bool = False):
    tb = slide.shapes.add_textbox(*(int(v) for v in box))
    tf = tb.text_frame
    tf.word_wrap = True
    r = tf.paragraphs[0].add_run()
    r.text = text
    _style_run(r, size=size, color=color, font=font, italic=italic, bold=bold)
    if align is not None:
        tf.paragraphs[0].alignment = align
    return tb


def _background(slide, st: dict) -> None:
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _rgb(st["bg"])


def _title_deco(slide, st: dict, geo: dict, accent: str) -> None:
    """The style's mark around a slide title: plain's short bar, classic's
    rule, business's band (+ stripe under it), modern's stripe at the left."""
    if st["name"] == "business":
        _rect(slide, geo["band"], accent, name=_ACCENT_BAR, back=True)
        _rect(slide, geo["stripe"], st.get("stripe") or _mix(accent, _WHITE, 0.45),
              name="Accent stripe")
        return
    _rect(slide, geo["bar"], accent, name=_ACCENT_BAR, flat=st["name"] != "plain")


def _body_shape(slide, *, create: bool, st: dict | None, box):
    for sh in slide.placeholders:
        if sh.placeholder_format.idx != 0 and _ph_type(sh) in (
                "body", "object", "subtitle") and sh.has_text_frame:
            return sh
    if not create:
        return None
    for ph in slide.slide_layout.placeholders:
        if ph.placeholder_format.idx != 0 and _ph_type(ph) in ("body", "object"):
            slide.shapes.clone_placeholder(ph)
            sh = next(s for s in slide.placeholders
                      if s.placeholder_format.idx == ph.placeholder_format.idx)
            if st:
                _place(sh, box)
            return sh
    sh = slide.shapes.add_textbox(*box)
    sh.name = "Body"
    return sh


def _drop_empty_placeholders(slide) -> None:
    """An unused placeholder shows "Click to add text" while editing."""
    for sh in list(slide.placeholders):
        if sh.has_text_frame and not sh.text_frame.text.strip() \
                and _ph_type(sh) not in _SKIP_PH:
            sh._element.getparent().remove(sh._element)


# ── pictures and tables ──────────────────────────────────────────────────
def _add_picture(slide, path: Path, box, *, exact: bool = False):
    x, y, w, h = box
    if exact:
        return slide.shapes.add_picture(str(path), x, y, w or None, h or None)
    pic = slide.shapes.add_picture(str(path), x, y)
    iw, ih = int(pic.width) or 1, int(pic.height) or 1
    s = min(w / iw, h / ih)
    pic.width, pic.height = int(iw * s), int(ih * s)
    pic.left = x + (w - int(pic.width)) // 2
    pic.top = y + (h - int(pic.height)) // 2
    return pic


def _table_rows(raw) -> list[list[str]]:
    rows = _json_list(raw, "table")
    out = []
    for r in rows:
        if isinstance(r, (list, tuple)):
            out.append(["" if c is None else str(c) for c in r])
        elif isinstance(r, dict):
            out.append(["" if c is None else str(c) for c in r.values()])
        else:
            out.append([str(r)])
    if not out or not max(len(r) for r in out):
        raise _OpError("table needs at least one row with one cell")
    return out


def _cell_grid(cell, color: str) -> None:
    """Thin cell borders in ``color`` (the default table style draws white
    lines, harsh on a dark slide). python-pptx has no API: tcPr XML."""
    from pptx.oxml.ns import qn
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        for old in tc_pr.findall(qn(tag)):
            tc_pr.remove(old)
    fill = tc_pr.find(qn("a:solidFill"))
    for i, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
        ln = tc_pr.makeelement(qn(tag), {"w": "12700", "cmpd": "sng"})
        sf = ln.makeelement(qn("a:solidFill"), {})
        sf.append(sf.makeelement(qn("a:srgbClr"), {"val": color}))
        ln.append(sf)
        # schema order: borders come before the cell fill
        if fill is not None:
            fill.addprevious(ln)
        else:
            tc_pr.append(ln)


def _add_table(slide, rows: list[list[str]], box, *, st: dict | None,
               accent: str, exact: bool = False):
    """A table; with a style: accent header (text in the colour that reads
    on it), body rows in the style's two banding fills."""
    x, y, w, h = box
    ncols = max(len(r) for r in rows)
    row_h = int(0.55 * _EMU_IN)
    height = h if exact else min(h, row_h * len(rows))
    gf = slide.shapes.add_table(len(rows), ncols, x, y, w, height)
    tbl = gf.table
    if st:
        tbl.horz_banding = False
    head_text = _on(accent)
    for ri, row in enumerate(rows):
        for ci in range(ncols):
            cell = tbl.cell(ri, ci)
            cell.text = row[ci] if ci < len(row) else ""
            if not st:
                continue
            head = ri == 0
            cell.fill.solid()
            fill = accent if head else (st["row_a"] if ri % 2 else st["row_b"])
            cell.fill.fore_color.rgb = _rgb(fill)
            if st["name"] != "plain":
                from pptx.enum.text import MSO_ANCHOR
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                if st.get("grid"):
                    _cell_grid(cell, st["grid"])
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    _style_run(run, size=st["table_pt"], font=st["body_font"],
                               color=head_text if head else st["row_text"], bold=head)
    return gf


# ── building one slide from an outline ───────────────────────────────────
def _outline(spec) -> dict:
    if isinstance(spec, str):
        return {"title": spec}
    if not isinstance(spec, dict):
        raise _OpError("each slide must be an object {title, bullets, ...}")
    return spec


def _is_cover(spec: dict, *, first: bool) -> bool:
    """A title slide: asked for (``layout: title`` / a ``subtitle``), or the
    deck's first slide with only a title (a real chat asked for a title slide
    "Q3 Review" with a subtitle; the tool had no subtitle, dropped it, and drew
    a small heading on an empty page)."""
    lay = str(spec.get("layout") or "").strip().lower()
    if lay in ("title", "title slide", "cover"):
        return True
    if lay:
        return False
    has_content = any(spec.get(k) for k in ("bullets", "body", "table", "image"))
    return not has_content and (bool(spec.get("subtitle")) or first)


def _cover_title(sh, box, title: str, st: dict, *, color: str, align, anchor) -> None:
    _place(sh, box)
    tf = sh.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    run = tf.paragraphs[0].add_run()
    run.text = title
    _style_run(run, size=st["cover_pt"], color=color,
               bold=st["title_bold"] or st["name"] != "classic", font=st["title_font"])
    tf.paragraphs[0].alignment = align


def _build_cover(prs, spec: dict, *, st: dict | None, accent: str):
    """The title slide in the style's cover layout:
    plain    — big centred title, short accent bar, grey subtitle;
    classic  — centred serif title between two thin rules, italic subtitle;
    business — an accent block over the left 40% holding title + subtitle;
    modern   — big left-aligned title, accent bar under it, tinted subtitle."""
    slide = prs.slides.add_slide(_pick_layout(prs, with_body=False))
    w, h = int(prs.slide_width), int(prs.slide_height)
    mx = int(w * 0.08)
    title = str(spec.get("title") or "")
    sub = str(spec.get("subtitle") or "")
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    sh = _title_shape(slide, create=True, st=st, geo=_geometry(prs, st))
    name = (st or {}).get("name")
    if st:
        _background(slide, st)
    if name == "plain":
        _cover_title(sh, (mx, int(h * 0.28), w - 2 * mx, int(h * 0.22)), title, st,
                     color=st["title"], align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
        bw = int(w * 0.12)
        _rect(slide, ((w - bw) // 2, int(h * 0.52), bw, max(int(h * 0.01), 1)),
              accent, name=_ACCENT_BAR, flat=False)
        if sub:
            _text_box(slide, (mx, int(h * 0.56), w - 2 * mx, int(h * 0.12)), sub,
                      font=st["body_font"], size=st["subtitle_pt"], color=st["muted"],
                      align=PP_ALIGN.CENTER)
    elif name == "classic":
        rw, rh = int(w * 0.56), max(int(h * 0.0035), 1)
        _rect(slide, ((w - rw) // 2, int(h * 0.29), rw, rh), accent, name=_ACCENT_BAR)
        _cover_title(sh, (mx, int(h * 0.31), w - 2 * mx, int(h * 0.22)), title, st,
                     color=st["title"], align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        _rect(slide, ((w - rw) // 2, int(h * 0.55), rw, rh), accent, name="Accent rule")
        if sub:
            _text_box(slide, (mx, int(h * 0.585), w - 2 * mx, int(h * 0.12)), sub,
                      font=st["body_font"], size=st["subtitle_pt"], color=st["muted"],
                      align=PP_ALIGN.CENTER, italic=True)
    elif name == "business":
        bw = int(w * 0.4)
        _rect(slide, (0, 0, bw, h), accent, name=_ACCENT_BAR, back=True)
        _rect(slide, (bw, 0, max(int(w * 0.012), 1), h),
              st.get("stripe") or _mix(accent, _WHITE, 0.45), name="Accent stripe")
        on = _on(accent)
        px = int(w * 0.05)
        _cover_title(sh, (px, int(h * 0.2), bw - 2 * px, int(h * 0.36)), title, st,
                     color=on, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.BOTTOM)
        _rect(slide, (px, int(h * 0.585), int(w * 0.06), max(int(h * 0.008), 1)),
              st.get("stripe") or _mix(accent, _WHITE, 0.45), name="Accent dash")
        if sub:
            _text_box(slide, (px, int(h * 0.61), bw - 2 * px, int(h * 0.2)), sub,
                      font=st["body_font"], size=st["subtitle_pt"],
                      color=_mix(on, accent, 0.22), align=PP_ALIGN.LEFT)
    elif name == "modern":
        _cover_title(sh, (mx, int(h * 0.24), w - 2 * mx, int(h * 0.28)), title, st,
                     color=st["title"], align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.BOTTOM)
        _rect(slide, (mx, int(h * 0.545), int(w * 0.1), max(int(h * 0.016), 1)),
              accent, name=_ACCENT_BAR)
        if sub:
            _text_box(slide, (mx, int(h * 0.59), w - 2 * mx, int(h * 0.12)), sub,
                      font=st["body_font"], size=st["subtitle_pt"],
                      color=_mix(st["muted"], accent, 0.35), align=PP_ALIGN.LEFT)
    else:
        _write_title(sh, title, st=None)
        if sub:
            body = _body_shape(slide, create=True, st=None, box=_geometry(prs)["body"])
            _write_bullets(body.text_frame, [(sub, 0)], st=None)
    notes = spec.get("notes")
    if notes:
        slide.notes_slide.notes_text_frame.text = str(notes)
    _drop_empty_placeholders(slide)
    return slide


def _build_slide(prs, spec: dict, *, st: dict | None, accent: str, cwd: str,
                 first: bool = False):
    """One slide from an outline entry; ``st`` None = the template's look."""
    spec = _outline(spec)
    if _is_cover(spec, first=first):
        return _build_cover(prs, spec, st=st, accent=accent)
    title = str(spec.get("title") or "")
    bullets = _flatten(spec.get("bullets") or spec.get("body") or [])
    img_raw = spec.get("image")
    table_raw = spec.get("table")
    image = None
    if img_raw:
        image = _resolve_existing(cwd, str(img_raw))
        if image is None:
            raise _OpError(f"image not found: {img_raw}")
    rows = _table_rows(table_raw) if table_raw else None

    layout = _pick_layout(prs, with_body=bool(bullets),
                          name=str(spec.get("layout") or ""))
    slide = prs.slides.add_slide(layout)
    geo = _geometry(prs, st)
    if st:
        _background(slide, st)

    side = [k for k, v in (("table", rows), ("image", image)) if v]
    body_box = geo["body"]
    side_box = geo["body"]
    if bullets and side:
        body_box, side_box = _split(geo["body"], 2, horizontal=True)

    if title:
        sh = _title_shape(slide, create=True, st=st, geo=geo)
        if st:
            _place(sh, geo["title"])
        _write_title(sh, title, st=st, accent=accent)
        if st:
            _title_deco(slide, st, geo, accent)
    if bullets:
        body = _body_shape(slide, create=True, st=st, box=body_box)
        if st:
            _place(body, body_box)
        _write_bullets(body.text_frame, bullets, st=st)
    boxes = _split(side_box, len(side), horizontal=False) if side else []
    for kind, box in zip(side, boxes, strict=True):
        if kind == "table":
            _add_table(slide, rows, box, st=st, accent=accent)
        else:
            _add_picture(slide, image, box)
    notes = spec.get("notes")
    if notes:
        slide.notes_slide.notes_text_frame.text = str(notes)
    _drop_empty_placeholders(slide)
    return slide


# ── slide list surgery ───────────────────────────────────────────────────
def _slide_no(prs, value, *, what: str = "slide", allow_end: bool = False) -> int:
    """A 1-based slide number (``-1`` = last) as a 0-based index."""
    n = len(prs.slides)
    try:
        v = int(value)
    except (TypeError, ValueError) as exc:
        raise _OpError(f"{what} must be a slide number (1 = first), got {value!r}") from exc
    hi = n + 1 if allow_end else n
    if v < 0:
        v = hi + 1 + v
    if not 1 <= v <= hi:
        raise _OpError(f"{what} {value} is out of range — the deck has {n} slide(s)")
    return v - 1


def _move(prs, src: int, dst: int) -> None:
    lst = prs.slides._sldIdLst
    el = list(lst)[src]
    lst.remove(el)
    lst.insert(dst, el)


def _delete(prs, idx: int) -> None:
    lst = prs.slides._sldIdLst
    el = list(lst)[idx]
    rid = el.rId
    lst.remove(el)
    prs.part.drop_rel(rid)


# ── replace_text ─────────────────────────────────────────────────────────
def _replace_in_para(para, find: str, repl: str) -> int:
    runs = para.runs
    full = "".join(r.text for r in runs)
    total = full.count(find)
    if not total:
        return 0
    if sum(r.text.count(find) for r in runs) == total:
        for r in runs:                       # each match inside one run:
            if find in r.text:               # that run keeps its formatting
                r.text = r.text.replace(find, repl)
        return total
    runs[0].text = full.replace(find, repl)  # a match spans runs: the first
    for r in runs[1:]:                       # run's formatting wins
        r.text = ""
    return total


def _text_frames(slide, *, notes: bool):
    for sh in _iter_shapes(slide.shapes):
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            yield sh.text_frame
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                for cell in row.cells:
                    yield cell.text_frame
    if notes and slide.has_notes_slide:
        yield slide.notes_slide.notes_text_frame


# ── pptx_read ────────────────────────────────────────────────────────────
def _shape_kind(sh) -> str:
    tag = sh._element.tag.rsplit("}", 1)[-1]
    if tag == "pic":
        return "picture"
    if tag == "grpSp":
        return "group"
    if getattr(sh, "has_chart", False) and sh.has_chart:
        return "chart"
    if getattr(sh, "has_table", False) and sh.has_table:
        return "table"
    if tag == "graphicFrame":
        return "graphic"
    if getattr(sh, "is_placeholder", False):
        return "placeholder"
    if tag == "cxnSp":
        return "connector"
    has_text = getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()
    return "text" if has_text else "shape"


def _tf_text(tf) -> str:
    return "\n".join("  " * p.level + "".join(r.text for r in p.runs)
                     for p in tf.paragraphs
                     if "".join(r.text for r in p.runs).strip())


def _read_slide(slide, number: int) -> dict:
    shapes = []
    flags = {"has_image": False, "has_table": False, "has_chart": False}
    for sh in _iter_shapes(slide.shapes):
        kind = _shape_kind(sh)
        info: dict = {"id": sh.shape_id, "name": sh.name, "type": kind}
        ph = _ph_type(sh)
        if ph:
            info["placeholder"] = ph
        if kind == "picture" or (ph == "picture" and sh._element.tag.endswith("}pic")):
            flags["has_image"] = True
        if kind == "table":
            flags["has_table"] = True
            info["rows"] = [[c.text for c in row.cells] for row in sh.table.rows]
        if kind == "chart":
            flags["has_chart"] = True
            with contextlib.suppress(Exception):
                info["chart_type"] = str(getattr(sh.chart.chart_type, "name",
                                                 sh.chart.chart_type)).lower()
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            text = _tf_text(sh.text_frame)
            if text:
                info["text"] = text
        shapes.append(info)
    title = slide.shapes.title
    notes = ""
    if slide.has_notes_slide:
        notes = slide.notes_slide.notes_text_frame.text.strip()
    return {"index": number, "layout": slide.slide_layout.name,
            "title": title.text_frame.text.strip() if title is not None else "",
            "shapes": shapes, "notes": notes, **flags}


def _t_pptx_read(args: dict, cwd: str) -> dict:
    """Slides of a deck as JSON: index, layout, title, every shape's text
    (with id and name), notes, and whether it holds images/tables/charts.
    Optional ``slides`` ("2-4", "1,3") limits which slides come back."""
    Presentation = _lib()
    if Presentation is None:
        return _missing()
    raw = _arg_path(args, "path", "file", "file_path", "filename")
    if not raw:
        return {"ok": False, "error": "need a .pptx path"}
    try:
        p = _resolve_existing(cwd, raw)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    if p is None:
        return {"ok": False, "error": f"file not found: {raw}"}
    try:
        prs = Presentation(str(p))
    except Exception as exc:  # noqa: BLE001 — not a pptx / corrupt
        return {"ok": False, "error": f"cannot open {p.name} as a .pptx: {exc}"}
    total = len(prs.slides)
    wanted = None
    spec = str(args.get("slides") or args.get("pages") or "").strip()
    if spec:
        from aiforge_core.runtime.doc_extract import parse_page_spec
        wanted = set(parse_page_spec(spec, total))
        if not wanted:
            return {"ok": False, "error": f"slides {spec!r} out of range — "
                                          f"{p.name} has {total} slide(s)"}
    slides, used, truncated = [], 0, False
    for i, slide in enumerate(prs.slides):
        if wanted is not None and i not in wanted:
            continue
        info = _read_slide(slide, i + 1)
        used += len(json.dumps(info, ensure_ascii=False))
        if used > _READ_BUDGET and slides:
            truncated = True
            break
        slides.append(info)
    out = {"ok": True, "path": str(p), "slide_count": total,
           "slide_size_in": [round(int(prs.slide_width) / _EMU_IN, 2),
                             round(int(prs.slide_height) / _EMU_IN, 2)],
           "layouts": [lo.name for lo in prs.slide_layouts],
           "slides": slides}
    meta = _get_meta(prs)
    if meta.get("style") in _STYLES:
        out["style"] = meta["style"]
        if meta.get("accent"):
            out["accent"] = meta["accent"]
    if truncated:
        out["truncated"] = (f"stopped after slide {slides[-1]['index']}: pass "
                            "slides (e.g. \"10-20\") to read the rest")
    return out


# ── pptx_create ──────────────────────────────────────────────────────────
def _t_pptx_create(args: dict, cwd: str) -> dict:
    """A new deck from an outline: ``slides`` = [{title, bullets[], notes?,
    image?, table?, layout?}]. ``style`` picks a built-in look (plain by
    default); ``template`` (a .pptx path or a saved template's name) reuses
    that deck's layouts and theme instead."""
    Presentation = _lib()
    if Presentation is None:
        return _missing()
    raw = _arg_path(args, "path", "file", "file_path", "out_path")
    if not raw:
        return {"ok": False, "error": "need a path for the new .pptx"}
    try:
        dest = _resolve(cwd, raw)
        slides = _json_list(args.get("slides"), "slides")
        if not slides:
            raise _OpError("slides is empty: give at least one {title, bullets}")
        style_arg = str(args.get("style") or "").strip()
        if style_arg and style_arg.lower() not in _STYLES:
            raise _OpError(_unknown_style(style_arg))
        style_arg = style_arg.lower()
        tpl_raw = _arg_path(args, "template")
        meta: dict = {}
        if tpl_raw:
            tpl = _find_template(cwd, tpl_raw)
            prs = Presentation(str(tpl))
            for i in range(len(prs.slides) - 1, -1, -1):
                _delete(prs, i)
            # a template saved from a styled deck keeps drawing that style
            meta = _get_meta(prs)
            sname = style_arg or (meta.get("style") if meta.get("style") in _STYLES else "")
        else:
            from pptx.util import Inches
            prs = Presentation()
            prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
            sname = style_arg or "plain"
        st = _STYLES[sname] if sname else None
        kept = meta.get("accent") if sname and sname == meta.get("style") else None
        accent = str(args.get("accent") or kept
                     or (st["accent"] if st else _ACCENT)).lstrip("#")
        _rgb(accent)
        for n, spec in enumerate(slides, 1):
            try:
                _build_slide(prs, spec, st=st, accent=accent, cwd=cwd, first=n == 1)
            except _OpError as exc:
                raise _OpError(f"slide {n}: {exc}") from exc
        if st:
            _set_meta(prs, sname, accent)
        existed = dest.exists()
        _save(prs, dest)
    except (_OpError, PermissionError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — bad template / unreadable image
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {"ok": True, "path": str(dest), "slides": len(prs.slides),
           "style": sname or "template"}
    if tpl_raw:
        out["template"] = str(tpl)
    if not existed:
        out["created"] = True
    return out


# ── templates ────────────────────────────────────────────────────────────
def _templates_dir() -> Path:
    base = os.environ.get("AIFORGE_CONFIG_DIR") or os.path.expanduser("~/.aiforge")
    return Path(base) / "pptx-templates"


def _template_names() -> list[str]:
    d = _templates_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.pptx")
                  if p.is_file() and _TPL_NAME.match(p.stem))


def _find_template(cwd: str, raw: str) -> Path:
    """A template: an existing .pptx path first, else a saved template of that
    name (``$AIFORGE_CONFIG_DIR/pptx-templates/<name>.pptx``)."""
    denied = None
    try:
        p = _resolve_existing(cwd, raw)
    except PermissionError as exc:
        p, denied = None, exc
    if p is not None:
        return p
    name = raw[:-5] if raw.lower().endswith(".pptx") else raw
    if _TPL_NAME.match(name):
        cand = _templates_dir() / f"{name}.pptx"
        if cand.is_file():
            return cand
    elif denied is not None:
        raise denied
    names = _template_names()
    raise _OpError(f"template not found: {raw}; saved templates: "
                   + (", ".join(names) if names
                      else "none (save one with pptx_save_template)"))


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


def _t_pptx_save_template(args: dict, cwd: str) -> dict:
    """Save a deck's layouts and theme as a named template (its slides are
    left out) for ``pptx_create`` ``template: <name>``."""
    Presentation = _lib()
    if Presentation is None:
        return _missing()
    raw = _arg_path(args, "path", "file", "file_path")
    name = str(args.get("name") or "").strip()
    if not raw:
        return {"ok": False, "error": "need the .pptx path to save as a template"}
    if name.lower().endswith(".pptx"):
        name = name[:-5]
    if not _TPL_NAME.match(name):
        return {"ok": False, "error": f"bad template name {name!r}: use 1-64 letters, "
                                      "digits, - or _"}
    try:
        src = _resolve_existing(cwd, raw)
        if src is None:
            raise _OpError(f"file not found: {raw}")
        dest = _templates_dir() / f"{name}.pptx"
        if dest.exists() and not _truthy(args.get("overwrite")):
            raise _OpError(f"template {name!r} already exists; pass overwrite: true "
                           "to replace it")
        prs = Presentation(str(src))
        dropped = len(prs.slides)
        for i in range(dropped - 1, -1, -1):
            _delete(prs, i)
        _save(prs, dest)
    except (_OpError, PermissionError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — not a pptx / unwritable folder
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {"ok": True, "name": name, "path": str(dest),
           "layouts": [lo.name for lo in prs.slide_layouts],
           "slides_left_out": dropped}
    style = _get_meta(prs).get("style")
    if style in _STYLES:
        out["style"] = style
    return out


def _t_pptx_templates(args: dict, cwd: str) -> dict:
    """The saved templates (name, path, slide size, layouts) and the built-in
    style names."""
    styles = [{"name": k, "look": v["desc"]} for k, v in _STYLES.items()]
    out: dict = {"ok": True, "folder": str(_templates_dir()), "styles": styles,
                 "templates": []}
    names = _template_names()
    if not names:
        return out
    Presentation = _lib()
    for name in names:
        p = _templates_dir() / f"{name}.pptx"
        info: dict = {"name": name, "path": str(p)}
        if Presentation is None:
            info["error"] = MISSING
        else:
            try:
                prs = Presentation(str(p))
                info["slide_size_in"] = [round(int(prs.slide_width) / _EMU_IN, 2),
                                         round(int(prs.slide_height) / _EMU_IN, 2)]
                info["layouts"] = [lo.name for lo in prs.slide_layouts]
                style = _get_meta(prs).get("style")
                if style in _STYLES:
                    info["style"] = style
            except Exception as exc:  # noqa: BLE001 — a broken file in the folder
                info["error"] = f"cannot open: {exc}"
        out["templates"].append(info)
    return out


# ── pptx_edit ────────────────────────────────────────────────────────────
def _op_slide(prs, op: dict) -> int:
    key = next((k for k in ("slide", "index", "at") if op.get(k) is not None), None)
    if key is None:
        raise _OpError("needs slide (1 = first)")
    return _slide_no(prs, op[key])


def _op_replace_text(prs, op, ctx) -> str:
    find = op.get("find", op.get("old"))
    repl = op.get("replace", op.get("new", ""))
    if not isinstance(find, str) or not find:
        raise _OpError("needs find (non-empty text)")
    only = None
    if op.get("slides") is not None or op.get("slide") is not None:
        sel = op.get("slides", op.get("slide"))
        sel = sel if isinstance(sel, list) else [sel]
        only = {_slide_no(prs, s) for s in sel}
    count = 0
    for i, slide in enumerate(prs.slides):
        if only is not None and i not in only:
            continue
        for tf in _text_frames(slide, notes=bool(op.get("notes"))):
            for para in tf.paragraphs:
                count += _replace_in_para(para, find, str(repl))
    if not count and op.get("required", True):
        raise _OpError(f"text {find!r} not found")
    return f"replaced {count} occurrence(s) of {find!r}"


def _op_set_title(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    slide = prs.slides[i]
    sh = _title_shape(slide, create=True, st=ctx["st"], geo=ctx["geo"])
    _write_title(sh, str(op.get("text", op.get("title", ""))), st=ctx["st"],
                 accent=ctx["accent"])
    return f"slide {i + 1}: title set"


def _op_set_bullets(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    slide = prs.slides[i]
    items = _flatten(_json_list(op.get("bullets", []), "bullets"))
    body = _body_shape(slide, create=True, st=ctx["st"], box=ctx["geo"]["body"])
    styles = None if ctx["st"] else _capture_styles(body.text_frame)
    _write_bullets(body.text_frame, items, st=ctx["st"], styles=styles)
    return f"slide {i + 1}: {len(items)} bullet(s) set"


def _op_set_notes(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    prs.slides[i].notes_slide.notes_text_frame.text = str(
        op.get("text", op.get("notes", "")))
    return f"slide {i + 1}: notes set"


def _op_add_slide(prs, op, ctx) -> str:
    spec = op.get("slide") if isinstance(op.get("slide"), dict) else op
    pos = next((op[k] for k in ("index", "at", "position") if op.get(k) is not None),
               None)
    if pos is None and not isinstance(op.get("slide"), dict) \
            and op.get("slide") is not None:
        pos = op.get("slide")
    dst = _slide_no(prs, pos, what="index", allow_end=True) if pos is not None \
        else len(prs.slides)
    _build_slide(prs, spec, st=ctx["st"], accent=ctx["accent"], cwd=ctx["cwd"])
    if dst != len(prs.slides) - 1:
        _move(prs, len(prs.slides) - 1, dst)
    return f"slide added at {dst + 1}"


def _op_delete_slide(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    _delete(prs, i)
    return f"slide {i + 1} deleted"


def _op_move_slide(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    to = op.get("to", op.get("to_index", op.get("position")))
    if to is None:
        raise _OpError("needs to (the new slide number)")
    dst = _slide_no(prs, to, what="to")
    _move(prs, i, dst)
    return f"slide {i + 1} moved to {dst + 1}"


def _op_box(op, ctx):
    """The op's own box in inches, else the content area below the title."""
    x, y = _inches(op.get("left")), _inches(op.get("top"))
    w, h = _inches(op.get("width")), _inches(op.get("height"))
    if x is None and y is None and w is None and h is None:
        return ctx["geo"]["body"], False
    bx, by, bw, bh = ctx["geo"]["body"]
    return (bx if x is None else x, by if y is None else y,
            w or 0, h or 0), True


def _op_add_image(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    raw = op.get("image") or op.get("path") or op.get("file")
    if not raw:
        raise _OpError("needs image (a local file path)")
    img = _resolve_existing(ctx["cwd"], str(raw))
    if img is None:
        raise _OpError(f"image not found: {raw}")
    box, exact = _op_box(op, ctx)
    _add_picture(prs.slides[i], img, box, exact=exact)
    return f"slide {i + 1}: image {img.name} added"


def _op_add_table(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    rows = _table_rows(op.get("rows", op.get("table")))
    box, exact = _op_box(op, ctx)
    if exact:
        bx, by, bw, bh = ctx["geo"]["body"]
        x, y, w, h = box
        box = (x, y, w or bw, h or bh)
        exact = bool(_inches(op.get("height")))
    _add_table(prs.slides[i], rows, box, st=ctx["st"], accent=ctx["accent"],
               exact=exact)
    return f"slide {i + 1}: {len(rows)}x{max(len(r) for r in rows)} table added"


_OPS = {
    "replace_text": _op_replace_text,
    "set_title": _op_set_title,
    "set_bullets": _op_set_bullets,
    "set_notes": _op_set_notes,
    "add_slide": _op_add_slide,
    "delete_slide": _op_delete_slide,
    "move_slide": _op_move_slide,
    "add_image": _op_add_image,
    "add_table": _op_add_table,
}


def _t_pptx_edit(args: dict, cwd: str) -> dict:
    """Apply ``ops`` to a deck, in order, and save it (in place, or to
    ``out_path``). All or nothing: when one op fails nothing is written."""
    Presentation = _lib()
    if Presentation is None:
        return _missing()
    raw = _arg_path(args, "path", "file", "file_path")
    if not raw:
        return {"ok": False, "error": "need the .pptx path"}
    done: list[str] = []
    try:
        src = _resolve_existing(cwd, raw)
        if src is None:
            raise _OpError(f"file not found: {raw}")
        out_raw = _arg_path(args, "out_path", "output")
        dest = _resolve(cwd, out_raw) if out_raw else src
        ops = _json_list(args.get("ops"), "ops")
        if not ops:
            raise _OpError("ops is empty")
        prs = Presentation(str(src))
        # the style the deck remembers; an older plain deck (no memory, 4:3
        # master in a 16:9 deck) is plain; else the template's own look
        sname = _get_meta(prs).get("style")
        if sname not in _STYLES:
            sname = "plain" if _needs_fit(prs) else ""
        st = _STYLES.get(sname) if sname else None
        ctx = {"st": st, "geo": _geometry(prs, st), "cwd": cwd,
               "accent": _deck_accent(prs) if st else _ACCENT}
        for n, op in enumerate(ops, 1):
            kind = str((op or {}).get("op") or (op or {}).get("type")
                       or (op or {}).get("action") or "") if isinstance(op, dict) else ""
            fn = _OPS.get(kind)
            if fn is None:
                raise _OpError(f"op {n}: unknown op {kind!r}; one of "
                               + ", ".join(_OPS))
            try:
                done.append(f"{kind}: {fn(prs, op, ctx)}")
            except _OpError as exc:
                raise _OpError(f"op {n} ({kind}): {exc}") from exc
        _save(prs, dest)
    except (_OpError, PermissionError) as exc:
        return {"ok": False, "error": str(exc), "applied_before_error": done,
                "saved": False}
    except Exception as exc:  # noqa: BLE001 — corrupt deck / unreadable image
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "applied_before_error": done, "saved": False}
    return {"ok": True, "path": str(dest), "slides": len(prs.slides),
            "changes": done}


__all__ = ["MISSING", "_t_pptx_read", "_t_pptx_create", "_t_pptx_edit",
           "_t_pptx_save_template", "_t_pptx_templates"]
