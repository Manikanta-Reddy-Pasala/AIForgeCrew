"""PowerPoint tools: read, create and edit ``.pptx`` decks.

``python-pptx`` is an OPTIONAL extra (``pip install 'aiforgecrew[pptx]'``). It
is imported inside each call, never at module load, so a host without it
starts and runs every other tool as before; these three return
``{"ok": False, "error": "python-pptx is not installed: ..."}`` instead.

Slide numbers are 1-based everywhere (``slide: 1`` is the first slide), the
way a person names them. Positions and sizes in ops are inches.

The default look of a new deck is plain: 16:9, white background, Calibri,
dark text and one accent colour (a short bar under each title and the table
header row). With ``template`` the template's layouts and theme are used and
no styling is forced.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

MISSING = "python-pptx is not installed: pip install 'aiforgecrew[pptx]'"

_DARK = "1F1F1F"
_BODY = "333333"
_WHITE = "FFFFFF"
_ACCENT = "1F4E79"
_FONT = "Calibri"
_TITLE_PT = 32
_BODY_PT = (22, 19, 17, 15)
_TABLE_PT = 14
_ACCENT_BAR = "Accent bar"
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


def _style_run(run, *, size: int, color: str, bold: bool = False) -> None:
    f = run.font
    f.name = _FONT
    f.size = _pt(size)
    f.bold = bold
    f.color.rgb = _rgb(color)


def _pt(n):
    from pptx.util import Pt
    return Pt(n)


# ── geometry ─────────────────────────────────────────────────────────────
def _geometry(prs) -> dict:
    """Title, accent-bar and content boxes for the slide size (EMU)."""
    w, h = int(prs.slide_width), int(prs.slide_height)
    mx = int(w * 0.045)
    title_top, title_h = int(h * 0.05), int(h * 0.135)
    body_top = int(h * 0.235)
    return {
        "title": (mx, title_top, w - 2 * mx, title_h),
        "bar": (mx, title_top + title_h + int(h * 0.006), int(w * 0.09),
                max(int(h * 0.008), 1)),
        "body": (mx, body_top, w - 2 * mx, h - body_top - int(h * 0.07)),
    }


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


def _write_bullets(tf, items: list[tuple[str, int]], *, plain: bool,
                   styles: dict | None = None) -> None:
    tf.clear()
    tf.word_wrap = True
    for i, (text, level) in enumerate(items):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.level = level
        run = para.add_run()
        run.text = text
        if plain:
            _style_run(run, size=_BODY_PT[min(level, len(_BODY_PT) - 1)],
                       color=_BODY)
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


def _write_title(sh, text: str, *, plain: bool) -> None:
    tf = sh.text_frame
    if plain or not tf.paragraphs[0].runs:
        tf.clear()
        run = tf.paragraphs[0].add_run()
        run.text = text
        if plain:
            _style_run(run, size=_TITLE_PT, color=_DARK, bold=True)
            from pptx.enum.text import PP_ALIGN
            tf.paragraphs[0].alignment = PP_ALIGN.LEFT
        return
    para = tf.paragraphs[0]
    para.runs[0].text = text
    for r in para.runs[1:]:
        r.text = ""
    for extra in tf.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)


def _title_shape(slide, *, create: bool, plain: bool, geo: dict):
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
    if plain:
        _place(sh, geo["title"])
    return sh


def _body_shape(slide, *, create: bool, plain: bool, box):
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
            if plain:
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


def _add_table(slide, rows: list[list[str]], box, *, plain: bool,
               accent: str, exact: bool = False):
    x, y, w, h = box
    ncols = max(len(r) for r in rows)
    row_h = int(0.42 * _EMU_IN)
    height = h if exact else min(h, row_h * len(rows))
    gf = slide.shapes.add_table(len(rows), ncols, x, y, w, height)
    tbl = gf.table
    if plain:
        tbl.horz_banding = False
    for ri, row in enumerate(rows):
        for ci in range(ncols):
            cell = tbl.cell(ri, ci)
            cell.text = row[ci] if ci < len(row) else ""
            if not plain:
                continue
            head = ri == 0
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(accent if head else _WHITE)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    _style_run(run, size=_TABLE_PT,
                               color=_WHITE if head else _BODY, bold=head)
    return gf


# ── building one slide from an outline ───────────────────────────────────
def _outline(spec) -> dict:
    if isinstance(spec, str):
        return {"title": spec}
    if not isinstance(spec, dict):
        raise _OpError("each slide must be an object {title, bullets, ...}")
    return spec


def _build_slide(prs, spec: dict, *, plain: bool, accent: str, cwd: str):
    spec = _outline(spec)
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
    geo = _geometry(prs)
    if plain:
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = _rgb(_WHITE)

    side = [k for k, v in (("table", rows), ("image", image)) if v]
    body_box = geo["body"]
    side_box = geo["body"]
    if bullets and side:
        body_box, side_box = _split(geo["body"], 2, horizontal=True)

    if title:
        sh = _title_shape(slide, create=True, plain=plain, geo=geo)
        if plain:
            _place(sh, geo["title"])
        _write_title(sh, title, plain=plain)
        if plain:
            from pptx.enum.shapes import MSO_SHAPE
            bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *geo["bar"])
            bar.name = _ACCENT_BAR
            bar.fill.solid()
            bar.fill.fore_color.rgb = _rgb(accent)
            bar.line.fill.background()
    if bullets:
        body = _body_shape(slide, create=True, plain=plain, box=body_box)
        if plain:
            _place(body, body_box)
        _write_bullets(body.text_frame, bullets, plain=plain)
    boxes = _split(side_box, len(side), horizontal=False) if side else []
    for kind, box in zip(side, boxes, strict=True):
        if kind == "table":
            _add_table(slide, rows, box, plain=plain, accent=accent)
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
    if truncated:
        out["truncated"] = (f"stopped after slide {slides[-1]['index']}: pass "
                            "slides (e.g. \"10-20\") to read the rest")
    return out


# ── pptx_create ──────────────────────────────────────────────────────────
def _t_pptx_create(args: dict, cwd: str) -> dict:
    """A new deck from an outline: ``slides`` = [{title, bullets[], notes?,
    image?, table?, layout?}]. Plain 16:9 look by default; ``template`` (a
    .pptx) reuses that deck's layouts and theme instead."""
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
        tpl_raw = _arg_path(args, "template")
        if tpl_raw:
            tpl = _resolve_existing(cwd, tpl_raw)
            if tpl is None:
                raise _OpError(f"template not found: {tpl_raw}")
            prs = Presentation(str(tpl))
            for i in range(len(prs.slides) - 1, -1, -1):
                _delete(prs, i)
            plain = False
        else:
            from pptx.util import Inches
            prs = Presentation()
            prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
            plain = True
        accent = str(args.get("accent") or _ACCENT).lstrip("#")
        _rgb(accent)
        for n, spec in enumerate(slides, 1):
            try:
                _build_slide(prs, spec, plain=plain, accent=accent, cwd=cwd)
            except _OpError as exc:
                raise _OpError(f"slide {n}: {exc}") from exc
        existed = dest.exists()
        _save(prs, dest)
    except (_OpError, PermissionError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — bad template / unreadable image
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {"ok": True, "path": str(dest), "slides": len(prs.slides),
           "style": "template" if tpl_raw else "plain 16:9"}
    if not existed:
        out["created"] = True
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
    sh = _title_shape(slide, create=True, plain=ctx["plain"], geo=ctx["geo"])
    _write_title(sh, str(op.get("text", op.get("title", ""))), plain=ctx["plain"])
    return f"slide {i + 1}: title set"


def _op_set_bullets(prs, op, ctx) -> str:
    i = _op_slide(prs, op)
    slide = prs.slides[i]
    items = _flatten(_json_list(op.get("bullets", []), "bullets"))
    body = _body_shape(slide, create=True, plain=ctx["plain"], box=ctx["geo"]["body"])
    styles = None if ctx["plain"] else _capture_styles(body.text_frame)
    _write_bullets(body.text_frame, items, plain=ctx["plain"], styles=styles)
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
    _build_slide(prs, spec, plain=ctx["plain"], accent=ctx["accent"], cwd=ctx["cwd"])
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
    _add_table(prs.slides[i], rows, box, plain=ctx["plain"], accent=ctx["accent"],
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
        plain = _needs_fit(prs)
        ctx = {"plain": plain, "geo": _geometry(prs), "cwd": cwd,
               "accent": _deck_accent(prs) if plain else _ACCENT}
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


__all__ = ["MISSING", "_t_pptx_read", "_t_pptx_create", "_t_pptx_edit"]
