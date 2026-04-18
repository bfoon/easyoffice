"""
apps/letterhead/docx_export.py

Pure-Python export — DOCX, PDF, PNG.
No Node.js required.

KEY DESIGN DECISIONS
────────────────────
• The exported files contain ONLY the letterhead header/footer — the body
  is left completely BLANK so the user can type their letter in Word or
  fill in their own content.  This matches the purpose of a letterhead.

• The header/footer rendering exactly mirrors the live HTML preview in
  builder.html for all 6 styles: classic, modern, bold, minimal, split,
  elegant.

• Three export formats:
    GET …/export/docx/  →  python-docx  (no Node.js)
    GET …/export/pdf/   →  reportlab    (no LibreOffice)
    GET …/export/png/   →  Pillow       (letterhead strip image)

Install (if not already):
    pip install python-docx reportlab Pillow

───────────────────────────────────────────────────────────────────────────
PDF HEADER FIX (April 2026)
───────────────────────────────────────────────────────────────────────────
The previous PDF export used a 50pt outer page margin and drew the top
accent bar *inside* that margin, which left white space around the
letterhead header and made the PDF look different from the live preview
in the builder.

This rewrite draws background shapes (accent bars, banners, panels)
edge-to-edge from x=0 to x=W, starting at y=H (the very top of the
page) — exactly matching the HTML preview where the header has no outer
margin.  Only the text/logo content respects an inner horizontal pad.
The footer is unchanged (it already worked correctly edge-to-edge).
"""

import io
import logging
import os

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from .models import LetterheadTemplate
from .views import _require_letterhead_permission

logger = logging.getLogger(__name__)


# ── Missing-dependency helper ─────────────────────────────────────────────
# Each exporter needs a specific third-party package. If a server doesn't
# have one installed, we raise this with a clear install hint so the view
# can return a friendly message instead of a raw ImportError.

class MissingDependencyError(RuntimeError):
    """Raised when a required export library isn't installed on the server."""
    pass


def _require(pkg_import: str, pip_name: str, feature: str):
    """
    Import a package at runtime and raise a friendly error if missing.

        doc_mod = _require('docx', 'python-docx', 'DOCX export')
    """
    import importlib
    try:
        return importlib.import_module(pkg_import)
    except ImportError as exc:
        raise MissingDependencyError(
            f"{feature} requires the '{pip_name}' Python package, which is "
            f"not installed on this server. Install it with: pip install {pip_name}"
        ) from exc


# ── Colour helpers ─────────────────────────────────────────────────────────

def _hex(h: str) -> str:
    """Strip # and return uppercase 6-char hex."""
    return h.lstrip("#").upper()

def _rgb(h: str) -> tuple:
    """'#1D9E75' → (29, 158, 117)"""
    h = _hex(h)
    return (int(h[0:2],16), int(h[2:4],16), int(h[4:6],16))

def _rl_color(h: str):
    from reportlab.lib.colors import Color
    r,g,b = _rgb(h)
    return Color(r/255, g/255, b/255)


# ── Logo path helper ────────────────────────────────────────────────────────

def _logo_path(template: LetterheadTemplate):
    if not template.logo:
        return None
    try:
        return template.logo.path
    except (ValueError, NotImplementedError):
        import tempfile, uuid as _u
        ext = os.path.splitext(template.logo.name)[1] or ".png"
        p = os.path.join(tempfile.gettempdir(), f"lh_{_u.uuid4().hex}{ext}")
        try:
            with template.logo.open("rb") as s, open(p,"wb") as d:
                d.write(s.read())
            return p
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# DOCX  (python-docx)
# ═══════════════════════════════════════════════════════════════════════════

def _build_docx(t: LetterheadTemplate) -> bytes:
    _require('docx', 'python-docx', 'DOCX export')
    from docx import Document
    from docx.shared import Pt, Cm, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()
    sec = doc.sections[0]
    sec.page_width  = Cm(21)
    sec.page_height = Cm(29.7)

    # Header/footer edge-to-edge (flush with page top / bottom) — matches
    # the PDF export and the live preview. The *body* still has a normal
    # margin so the user has room to type a letter.
    sec.header_distance = Cm(0)
    sec.footer_distance = Cm(0)

    # top/bottom margin = where the body text starts/ends. Keep these
    # generous enough that body text doesn't collide with the
    # header/footer content (which sits in the 0..top_margin zone).
    sec.top_margin    = Cm(3.2)
    sec.bottom_margin = Cm(2.0)
    sec.left_margin   = Cm(2.54)
    sec.right_margin  = Cm(2.54)

    pri = _hex(t.color_primary  or "#1D9E75")
    sec_col = _hex(t.color_secondary or "#2C2C2A")
    pri_rgb = RGBColor(*_rgb(t.color_primary  or "#1D9E75"))
    sec_rgb = RGBColor(*_rgb(t.color_secondary or "#2C2C2A"))
    fh = (t.font_header or "Georgia").strip("'")
    fb = (t.font_body   or "Arial"  ).strip("'")
    k  = t.template_key or "classic"

    hdr = sec.header
    hdr.is_linked_to_previous = False
    ftr = sec.footer
    ftr.is_linked_to_previous = False

    # ── helper: add a paragraph to a container ──────────────────────────
    def para(container, text="", bold=False, italic=False, size=11,
             color_rgb=None, align=WD_ALIGN_PARAGRAPH.LEFT,
             font=None, sp_before=0, sp_after=4):
        p = container.add_paragraph()
        p.alignment = align
        p.paragraph_format.space_before = Pt(sp_before)
        p.paragraph_format.space_after  = Pt(sp_after)
        if text:
            r = p.add_run(text)
            r.bold   = bold
            r.italic = italic
            r.font.name = font or fb
            r.font.size = Pt(size)
            if color_rgb:
                r.font.color.rgb = color_rgb
        return p

    # ── helper: horizontal rule on a paragraph (bottom border) ──────────
    def _hrule(p, color_hex, size=6, space=4, position="bottom"):
        pPr  = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        el   = OxmlElement(f"w:{position}")
        el.set(qn("w:val"),   "single")
        el.set(qn("w:sz"),    str(size))
        el.set(qn("w:space"), str(space))
        el.set(qn("w:color"), color_hex)
        pBdr.append(el)
        pPr.append(pBdr)

    # ── helper: stretch a paragraph edge-to-edge ─────────────────────────
    # Uses negative indents to push the paragraph past the section's
    # left/right margins so its content (background shading, borders, and
    # text if you want) spans the full page width. The body text is
    # unaffected because it uses the normal section margins.
    #
    # Optional inner_pad_cm re-adds horizontal padding inside the stretched
    # paragraph so text doesn't sit flush against the paper edge, while
    # the background colour/border still extends to the edge.
    LEFT_M  = 2.54   # cm — matches sec.left_margin above
    RIGHT_M = 2.54
    def _edge_to_edge(p, inner_pad_cm=0.8):
        pf = p.paragraph_format
        pf.left_indent  = Cm(-LEFT_M  + inner_pad_cm)
        pf.right_indent = Cm(-RIGHT_M + inner_pad_cm)

    # ── helper: shade a paragraph background ────────────────────────────
    def _shade_para(p, fill_hex):
        pPr = p._p.get_or_add_pPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"),   "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"),  fill_hex)
        pPr.append(shd)

    # ── helper: shade a table cell ──────────────────────────────────────
    def _shade_cell(cell, fill_hex):
        tc   = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd  = OxmlElement("w:shd")
        shd.set(qn("w:val"),   "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"),  fill_hex)
        tcPr.append(shd)

    # ── helper: set table cell borders to none ───────────────────────────
    def _no_borders(tbl):
        tbl_element = tbl._tbl
        tbl_pr = tbl_element.find(qn("w:tblPr"))
        if tbl_pr is None:
            tbl_pr = OxmlElement("w:tblPr")
            tbl_element.insert(0, tbl_pr)
        bdr    = OxmlElement("w:tblBorders")
        for side in ("top","left","bottom","right","insideH","insideV"):
            el = OxmlElement(f"w:{side}")
            el.set(qn("w:val"),   "none")
            el.set(qn("w:sz"),    "0")
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "auto")
            bdr.append(el)
        tbl_pr.append(bdr)

    # ── helper: try to insert logo image ────────────────────────────────
    def _add_logo(run, height_inches=0.5):
        lp = _logo_path(t)
        if lp:
            try:
                run.add_picture(lp, height=Inches(height_inches))
                return True
            except Exception:
                pass
        return False

    # ── address lines ────────────────────────────────────────────────────
    addr_lines = [l.strip() for l in (t.address or "").split("\n") if l.strip()]
    addr_flat  = "  ·  ".join(addr_lines)

    # ════════════════════════════════════════════════════════
    # CLASSIC  — top accent bar, name left, address right, footer band
    # ════════════════════════════════════════════════════════
    if k == "classic":
        # Top rule paragraph — edge-to-edge (inner_pad=0 so the rule spans
        # the entire page width, just like the PDF's c.rect(0, H-5, W, 5))
        p_top = para(hdr, sp_before=0, sp_after=4)
        _edge_to_edge(p_top, inner_pad_cm=0)
        _hrule(p_top, pri, size=18, space=2, position="top")

        # Logo + name
        p_name = para(hdr, sp_before=4, sp_after=2)
        r_logo = p_name.add_run()
        has_logo = _add_logo(r_logo, 0.5)
        if has_logo:
            p_name.add_run("  ")
        r_name = p_name.add_run(t.company_name or "")
        r_name.bold = True; r_name.font.name = fh; r_name.font.size = Pt(20)
        r_name.font.color.rgb = sec_rgb

        if t.tagline:
            p_tag = para(hdr, t.tagline, italic=True, size=11, color_rgb=pri_rgb, sp_after=2)

        for line in addr_lines:
            para(hdr, line, size=9, color_rgb=RGBColor(0x88,0x88,0x88),
                 align=WD_ALIGN_PARAGRAPH.RIGHT, sp_after=0)
        if t.contact_info:
            para(hdr, t.contact_info, size=9, color_rgb=RGBColor(0xaa,0xaa,0xaa),
                 align=WD_ALIGN_PARAGRAPH.RIGHT, sp_after=4)

        # Divider
        p_div = para(hdr, sp_before=2, sp_after=0)
        _hrule(p_div, "DDDDDD", size=4, space=2)

        # Footer band — edge-to-edge coloured band with text inside
        p_ftr = para(ftr, f"{t.contact_info or ''}  ·  {addr_flat}",
                     size=9, color_rgb=RGBColor(255,255,255),
                     align=WD_ALIGN_PARAGRAPH.CENTER, sp_before=4, sp_after=4)
        _edge_to_edge(p_ftr, inner_pad_cm=0)
        _shade_para(p_ftr, pri)
        _hrule(p_ftr, pri, size=6, space=4, position="top")

    # ════════════════════════════════════════════════════════
    # MODERN  — left accent border, name, contact right, thin rule
    # ════════════════════════════════════════════════════════
    elif k == "modern":
        p_name = para(hdr, sp_before=4, sp_after=2)
        # Stretch to the left page edge so the accent stripe is flush-left.
        # inner_pad=0 on the left side; we use our own left_indent below.
        _edge_to_edge(p_name, inner_pad_cm=0)
        # Left border via paragraph border (becomes a flush-left stripe)
        pPr  = p_name._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        left = OxmlElement("w:left")
        left.set(qn("w:val"),   "single")
        left.set(qn("w:sz"),    "24")
        left.set(qn("w:space"), "12")
        left.set(qn("w:color"), pri)
        pBdr.append(left)
        pPr.append(pBdr)
        # Use Cm (not Pt) so we can override the negative left_indent we
        # just set via _edge_to_edge and push the text back in from the
        # left edge without re-triggering that helper's logic.
        p_name.paragraph_format.left_indent = Cm(0.5)

        r_logo = p_name.add_run()
        has_logo = _add_logo(r_logo, 0.45)
        if has_logo:
            p_name.add_run("  ")
        r_name = p_name.add_run(t.company_name or "")
        r_name.bold = True; r_name.font.name = fh; r_name.font.size = Pt(18)
        r_name.font.color.rgb = sec_rgb

        if t.tagline:
            p_tag = para(hdr, t.tagline, italic=True, size=10,
                         color_rgb=RGBColor(0xaa,0xaa,0xaa), sp_after=2)
            p_tag.paragraph_format.left_indent = Pt(16)

        if t.contact_info:
            para(hdr, t.contact_info, size=9, color_rgb=RGBColor(0x88,0x88,0x88),
                 align=WD_ALIGN_PARAGRAPH.RIGHT, sp_after=0)
        for line in addr_lines:
            para(hdr, line, size=9, color_rgb=RGBColor(0xbb,0xbb,0xbb),
                 align=WD_ALIGN_PARAGRAPH.RIGHT, sp_after=0)

        p_div = para(hdr, sp_before=4, sp_after=0)
        _hrule(p_div, "E0E0E0", size=4, space=2)

        # Footer rule spans the full width
        p_ftr = para(ftr, f"{t.contact_info or ''}  ·  {addr_flat}",
                     size=9, color_rgb=pri_rgb,
                     align=WD_ALIGN_PARAGRAPH.CENTER, sp_before=4, sp_after=4)
        _edge_to_edge(p_ftr, inner_pad_cm=0)
        _hrule(p_ftr, pri, size=6, space=4, position="top")

    # ════════════════════════════════════════════════════════
    # BOLD  — dark shaded header, accent rule under it
    # ════════════════════════════════════════════════════════
    elif k == "bold":
        # Name paragraph — dark banner edge-to-edge. inner_pad_cm > 0 so
        # text has some breathing room inside the banner.
        p_name = para(hdr, sp_before=0, sp_after=2)
        _edge_to_edge(p_name, inner_pad_cm=0.8)
        _shade_para(p_name, sec_col)
        r_logo = p_name.add_run()
        has_logo = _add_logo(r_logo, 0.48)
        if has_logo:
            p_name.add_run("  ")
        r_name = p_name.add_run(t.company_name or "")
        r_name.bold = True; r_name.font.name = fh; r_name.font.size = Pt(20)
        r_name.font.color.rgb = RGBColor(255,255,255)

        if t.tagline:
            p_tag = para(hdr, t.tagline, italic=True, size=10,
                         color_rgb=RGBColor(0xaa,0xaa,0xaa), sp_after=0)
            _edge_to_edge(p_tag, inner_pad_cm=0.8)
            _shade_para(p_tag, sec_col)

        if t.contact_info:
            p_c = para(hdr, t.contact_info, size=9,
                       color_rgb=RGBColor(0x99,0x99,0x99), sp_after=0)
            _edge_to_edge(p_c, inner_pad_cm=0.8)
            _shade_para(p_c, sec_col)

        # Accent rule edge-to-edge
        p_acc = para(hdr, sp_before=0, sp_after=8)
        _edge_to_edge(p_acc, inner_pad_cm=0)
        _hrule(p_acc, pri, size=12, space=0)

        # Footer dark band edge-to-edge
        p_ftr = para(ftr, f"{t.company_name or ''}  ·  {addr_flat}  ·  {t.contact_info or ''}",
                     size=9, color_rgb=RGBColor(0x99,0x99,0x99),
                     align=WD_ALIGN_PARAGRAPH.CENTER, sp_before=4, sp_after=4)
        _edge_to_edge(p_ftr, inner_pad_cm=0)
        _shade_para(p_ftr, sec_col)

    # ════════════════════════════════════════════════════════
    # MINIMAL  — name and contact inline, thin rule
    # ════════════════════════════════════════════════════════
    elif k == "minimal":
        p_name = para(hdr, sp_before=4, sp_after=2)
        r_logo = p_name.add_run()
        has_logo = _add_logo(r_logo, 0.42)
        if has_logo:
            p_name.add_run("  ")
        r_name = p_name.add_run(t.company_name or "")
        r_name.bold = True; r_name.font.name = fh; r_name.font.size = Pt(17)
        r_name.font.color.rgb = sec_rgb
        if t.tagline:
            p_name.add_run(f"  ·  {t.tagline}").font.color.rgb = RGBColor(0xbb,0xbb,0xbb)

        contact_line = addr_flat
        if t.contact_info:
            contact_line = f"{addr_flat}  ·  {t.contact_info}" if addr_flat else t.contact_info
        para(hdr, contact_line, size=9, color_rgb=RGBColor(0xbb,0xbb,0xbb),
             align=WD_ALIGN_PARAGRAPH.RIGHT, sp_after=2)

        # Divider rule spans the full page width
        p_div = para(hdr, sp_before=2, sp_after=0)
        _edge_to_edge(p_div, inner_pad_cm=0)
        _hrule(p_div, sec_col, size=4, space=2)

        para(ftr, f"{t.contact_info or ''}  |  {addr_flat}",
             size=9, color_rgb=RGBColor(0xbb,0xbb,0xbb),
             align=WD_ALIGN_PARAGRAPH.CENTER, sp_before=4, sp_after=4)

    # ════════════════════════════════════════════════════════
    # SPLIT  — two-column table: coloured left sidebar, white right
    # ════════════════════════════════════════════════════════
    elif k == "split":
        from docx.shared import Cm as _Cm
        tbl = hdr.add_table(rows=1, cols=2, width=_Cm(20.8))
        _no_borders(tbl)
        # Widen the table to span the full page width (21 cm - 0.2 cm safety).
        # Left cell = 5.5 cm (coloured sidebar), right cell = the rest.
        tbl.columns[0].width = _Cm(5.5)
        tbl.columns[1].width = _Cm(15.3)

        # Indent the table by -LEFT_M so it starts at the left page edge.
        # Uses w:tblPr/w:tblInd which supports negative values in OOXML.
        # CT_Tbl doesn't expose get_or_add_tblPr(); access via the element tree.
        tbl_element = tbl._tbl
        tbl_pr = tbl_element.find(qn("w:tblPr"))
        if tbl_pr is None:
            tbl_pr = OxmlElement("w:tblPr")
            tbl_element.insert(0, tbl_pr)
        tbl_ind = OxmlElement("w:tblInd")
        # w:w is in twips (1/20 pt). 2.54 cm = 1 in = 1440 twips, negated.
        tbl_ind.set(qn("w:w"),    str(-int(LEFT_M * 567)))   # ~-1440 twips
        tbl_ind.set(qn("w:type"), "dxa")
        tbl_pr.append(tbl_ind)
        # Also set the table width explicitly to the full page width so it
        # doesn't get auto-shrunk.
        tbl_w = OxmlElement("w:tblW")
        tbl_w.set(qn("w:w"),    str(int(20.8 * 567)))        # ~20.8 cm
        tbl_w.set(qn("w:type"), "dxa")
        tbl_pr.append(tbl_w)

        cl = tbl.cell(0,0)
        cr = tbl.cell(0,1)
        _shade_cell(cl, pri)

        # Left cell content (add a bit of inner padding via paragraph indent)
        p_logo = cl.paragraphs[0]
        p_logo.paragraph_format.left_indent = Cm(0.3)
        r_logo = p_logo.add_run()
        _add_logo(r_logo, 0.5)
        p_logo.paragraph_format.space_after = Pt(4)

        p_cl_name = cl.add_paragraph()
        p_cl_name.paragraph_format.left_indent = Cm(0.3)
        r_cn = p_cl_name.add_run(t.company_name or "")
        r_cn.bold = True; r_cn.font.name = fh; r_cn.font.size = Pt(13)
        r_cn.font.color.rgb = RGBColor(255,255,255)

        if t.tagline:
            p_cl_tag = cl.add_paragraph()
            p_cl_tag.paragraph_format.left_indent = Cm(0.3)
            r_ct = p_cl_tag.add_run(t.tagline)
            r_ct.font.size = Pt(9); r_ct.italic = True
            r_ct.font.color.rgb = RGBColor(0xdd,0xdd,0xdd)

        # Right cell content — keep a small right padding so it's not flush
        for line in addr_lines:
            p_r = cr.add_paragraph(line)
            p_r.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p_r.paragraph_format.right_indent = Cm(0.5)
            p_r.runs[0].font.size = Pt(9)
            p_r.runs[0].font.color.rgb = RGBColor(0x88,0x88,0x88)
        if t.contact_info:
            p_rc = cr.add_paragraph(t.contact_info)
            p_rc.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p_rc.paragraph_format.right_indent = Cm(0.5)
            p_rc.runs[0].font.size = Pt(9)
            p_rc.runs[0].font.color.rgb = RGBColor(0xaa,0xaa,0xaa)

        para(ftr, f"{t.contact_info or ''}  ·  {addr_flat}",
             size=9, color_rgb=RGBColor(0xaa,0xaa,0xaa),
             align=WD_ALIGN_PARAGRAPH.CENTER, sp_before=4, sp_after=4)

    # ════════════════════════════════════════════════════════
    # ELEGANT  — centred logo, name, double rule, contact line
    # ════════════════════════════════════════════════════════
    elif k == "elegant":
        if _logo_path(t):
            p_logo = para(hdr, sp_before=4, sp_after=4, align=WD_ALIGN_PARAGRAPH.CENTER)
            r_logo = p_logo.add_run()
            _add_logo(r_logo, 0.55)

        p_name = para(hdr, t.company_name or "", bold=True, size=22, font=fh,
                      color_rgb=sec_rgb, align=WD_ALIGN_PARAGRAPH.CENTER, sp_after=3)
        if t.tagline:
            p_tag = para(hdr, t.tagline, italic=True, size=11, color_rgb=pri_rgb,
                         align=WD_ALIGN_PARAGRAPH.CENTER, sp_after=6)

        # Double rule = thick rule then thin rule (both edge-to-edge)
        p_r1 = para(hdr, sp_before=2, sp_after=0)
        _edge_to_edge(p_r1, inner_pad_cm=0)
        _hrule(p_r1, pri, size=12, space=2)
        p_r2 = para(hdr, sp_before=0, sp_after=4)
        _edge_to_edge(p_r2, inner_pad_cm=0)
        _hrule(p_r2, pri, size=4, space=2)

        contact_line = addr_flat
        if t.contact_info:
            contact_line = f"{addr_flat}  ·  {t.contact_info}" if addr_flat else t.contact_info
        para(hdr, contact_line, size=9, color_rgb=RGBColor(0xaa,0xaa,0xaa),
             align=WD_ALIGN_PARAGRAPH.CENTER, sp_after=4)

        # Double rule footer (edge-to-edge)
        p_f1 = para(ftr, sp_before=4, sp_after=0)
        _edge_to_edge(p_f1, inner_pad_cm=0)
        _hrule(p_f1, pri, size=12, space=2, position="top")
        para(ftr, t.contact_info or "", size=9, color_rgb=RGBColor(0xaa,0xaa,0xaa),
             align=WD_ALIGN_PARAGRAPH.CENTER, sp_before=2, sp_after=4)

    else:
        # Fallback to classic
        p_name = para(hdr, t.company_name or "", bold=True, size=18, font=fh, color_rgb=sec_rgb)
        p_div = para(hdr, sp_before=2, sp_after=0)
        _edge_to_edge(p_div, inner_pad_cm=0)
        _hrule(p_div, pri, size=6, space=2)
        para(ftr, t.contact_info or "", size=9, color_rgb=pri_rgb,
             align=WD_ALIGN_PARAGRAPH.CENTER)

    # ── Body: ONE blank paragraph so cursor lands in the right place ─────
    doc.add_paragraph()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# PDF  (reportlab — no LibreOffice)
# ═══════════════════════════════════════════════════════════════════════════
#
# IMPORTANT LAYOUT NOTE:
# ReportLab's coordinate system has (0,0) at the BOTTOM-LEFT of the page.
# The TOP of the page is y=H (≈841.89pt for A4). The HTML preview draws
# the letterhead header starting from the very top of the page with no
# outer margin, and the coloured accent shapes (top rule, banner, left
# panel) extend edge-to-edge. To match that, background shapes in this
# PDF export MUST draw from x=0..W and start at y=H (no subtraction of
# an outer margin). Only the text/logo content uses an inner horizontal
# pad (PAD_X) so it's not flush with the page edge.
#
def _build_pdf(t: LetterheadTemplate) -> bytes:
    _require('reportlab', 'reportlab', 'PDF export')
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.colors import HexColor, white, Color
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    W, H = A4          # 595.27 × 841.89 pt

    # INNER horizontal pad for text/logo content (matches the HTML
    # preview's padding:14px 28px — at A4 ~595pt wide, 28px ≈ 28pt).
    PAD_X = 28.0

    pri = HexColor(t.color_primary  or "#1D9E75")
    sec = HexColor(t.color_secondary or "#2C2C2A")
    fh  = (t.font_header or "Georgia").strip("'")
    fb  = (t.font_body   or "Arial"  ).strip("'")
    k   = t.template_key or "classic"

    # Map CSS font names → ReportLab built-ins
    def rl_font(name, bold=False, italic=False):
        n = name.lower().strip("'")
        serif   = ("Times-Roman","Times-Bold","Times-Italic","Times-BoldItalic")
        mono    = ("Courier","Courier-Bold","Courier-Oblique","Courier-BoldOblique")
        sans    = ("Helvetica","Helvetica-Bold","Helvetica-Oblique","Helvetica-BoldOblique")
        if any(x in n for x in ("georgia","times","palatino","garamond")):
            fonts = serif
        elif any(x in n for x in ("courier","mono")):
            fonts = mono
        else:
            fonts = sans
        idx = (1 if bold else 0) + (2 if italic else 0)
        return fonts[min(idx,3)]

    font_h  = rl_font(fh, bold=True)
    font_h_ = rl_font(fh, bold=False)
    font_b  = rl_font(fb)
    font_bi = rl_font(fb, italic=True)

    c = rl_canvas.Canvas(buf, pagesize=A4)

    # Logo helper
    logo_img = None
    lp = _logo_path(t)
    if lp:
        try:
            logo_img = ImageReader(lp)
        except Exception:
            logo_img = None

    addr_lines = [l.strip() for l in (t.address or "").split("\n") if l.strip()]
    addr_flat  = "  ·  ".join(addr_lines)

    def draw_footer_band():
        """Coloured footer band edge-to-edge at the bottom of the page."""
        parts = []
        if t.contact_info: parts.append(t.contact_info)
        if addr_flat:      parts.append(addr_flat)
        text = "  ·  ".join(parts)
        c.setFillColor(pri)
        c.rect(0, 0, W, 28, stroke=0, fill=1)
        c.setFont(font_b, 8)
        c.setFillColor(white)
        c.drawCentredString(W/2, 10, text[:110])

    # ════════════════════════════════════════════════════════
    # CLASSIC — full-width top accent bar, then padded content
    # ════════════════════════════════════════════════════════
    if k == "classic":
        # Full-width top accent bar (5pt tall, flush to top of page)
        bar_h = 5
        c.setFillColor(pri)
        c.rect(0, H - bar_h, W, bar_h, stroke=0, fill=1)

        # Content starts just below the bar
        y = H - bar_h - 14     # ~14pt top padding inside the header

        # Logo + name (left side, with inner pad)
        lx = PAD_X
        if logo_img:
            lh_ = 42
            lw_ = 42
            c.drawImage(logo_img, lx, y - lh_, height=lh_, width=lw_,
                        preserveAspectRatio=True, mask="auto")
            lx += lw_ + 10

        c.setFont(font_h, 20); c.setFillColor(sec)
        c.drawString(lx, y - 20, t.company_name or "")

        if t.tagline:
            c.setFont(font_bi, 11); c.setFillColor(pri)
            c.drawString(lx, y - 34, t.tagline)

        # Address block on the right
        ay = y - 14
        c.setFont(font_b, 9); c.setFillColor(Color(0.53,0.53,0.53))
        for line in addr_lines:
            c.drawRightString(W - PAD_X, ay, line); ay -= 12
        if t.contact_info:
            c.setFillColor(Color(0.67,0.67,0.67))
            c.drawRightString(W - PAD_X, ay - 2, t.contact_info)

        # Thin divider under the header block
        div_y = y - 58
        c.setStrokeColor(Color(0.87,0.87,0.87)); c.setLineWidth(0.8)
        c.line(PAD_X, div_y, W - PAD_X, div_y)

        draw_footer_band()

    # ════════════════════════════════════════════════════════
    # MODERN — left accent stripe, padded content, thin rule, coloured footer text
    # ════════════════════════════════════════════════════════
    elif k == "modern":
        # Full-width region for the header (no top accent bar for modern)
        y = H - 14   # small top pad

        # Left accent stripe — short vertical bar from top edge
        stripe_w = 6
        stripe_h = 80
        c.setFillColor(pri)
        c.rect(0, H - stripe_h - 4, stripe_w, stripe_h, stroke=0, fill=1)

        lx = PAD_X
        if logo_img:
            c.drawImage(logo_img, lx, y - 42, height=38, width=38,
                        preserveAspectRatio=True, mask="auto")
            lx += 46

        c.setFont(font_h, 18); c.setFillColor(sec)
        c.drawString(lx, y - 18, t.company_name or "")
        if t.tagline:
            c.setFont(font_bi, 10); c.setFillColor(Color(0.67,0.67,0.67))
            c.drawString(lx, y - 32, t.tagline)

        ay = y - 14
        c.setFont(font_b, 9); c.setFillColor(Color(0.53,0.53,0.53))
        if t.contact_info:
            c.drawRightString(W - PAD_X, ay, t.contact_info); ay -= 12
        for line in addr_lines:
            c.drawRightString(W - PAD_X, ay, line); ay -= 12

        # Thin divider under the header
        div_y = y - 70
        c.setStrokeColor(Color(0.88,0.88,0.88)); c.setLineWidth(0.8)
        c.line(PAD_X, div_y, W - PAD_X, div_y)

        # Footer — accent line across, then coloured centred text
        parts = [t.contact_info or "", addr_flat]
        c.setStrokeColor(pri); c.setLineWidth(1.5)
        c.line(0, 32, W, 32)
        c.setFont(font_b, 8); c.setFillColor(pri)
        c.drawCentredString(W/2, 18, "  ·  ".join([p for p in parts if p])[:110])

    # ════════════════════════════════════════════════════════
    # BOLD — full-width dark banner at the very top, accent rule under it
    # ════════════════════════════════════════════════════════
    elif k == "bold":
        banner_h = 90 + (12 if t.tagline else 0) + (12 if t.contact_info else 0)
        # Full-width dark banner from top
        c.setFillColor(sec)
        c.rect(0, H - banner_h, W, banner_h, stroke=0, fill=1)

        # Accent rule right under the banner (full width)
        c.setFillColor(pri)
        c.rect(0, H - banner_h - 5, W, 5, stroke=0, fill=1)

        # Logo + name inside banner, padded from left
        lx = PAD_X
        ly_top = H - 16   # top of content inside banner
        if logo_img:
            c.drawImage(logo_img, lx, ly_top - 44, height=44, width=44,
                        preserveAspectRatio=True, mask="auto")
            lx += 54

        c.setFont(font_h, 20); c.setFillColor(white)
        c.drawString(lx, ly_top - 24, t.company_name or "")

        ty = ly_top - 42
        if t.tagline:
            c.setFont(font_bi, 10); c.setFillColor(Color(0.67,0.67,0.67))
            c.drawString(lx, ty, t.tagline)
            ty -= 14
        if t.contact_info:
            c.setFont(font_b, 9); c.setFillColor(Color(0.6,0.6,0.6))
            c.drawString(lx, ty, t.contact_info)

        # Dark footer band (matches banner)
        c.setFillColor(sec); c.rect(0, 0, W, 28, stroke=0, fill=1)
        c.setFont(font_b, 8); c.setFillColor(Color(0.6,0.6,0.6))
        footer_text = f"{t.company_name or ''}  ·  {addr_flat}  ·  {t.contact_info or ''}"
        c.drawCentredString(W/2, 10, footer_text[:110])

    # ════════════════════════════════════════════════════════
    # MINIMAL — name left, contact right, thin bottom rule (no top bar)
    # ════════════════════════════════════════════════════════
    elif k == "minimal":
        y = H - 14   # small top pad

        lx = PAD_X
        if logo_img:
            c.drawImage(logo_img, lx, y - 40, height=36, width=36,
                        preserveAspectRatio=True, mask="auto")
            lx += 44

        c.setFont(font_h, 17); c.setFillColor(sec)
        c.drawString(lx, y - 18, t.company_name or "")
        if t.tagline:
            c.setFont(rl_font(fh, italic=True), 10); c.setFillColor(Color(0.73,0.73,0.73))
            c.drawString(lx, y - 30, f"  {t.tagline}")

        contact_line = addr_flat
        if t.contact_info:
            contact_line = f"{addr_flat}  ·  {t.contact_info}" if addr_flat else t.contact_info
        c.setFont(font_b, 9); c.setFillColor(Color(0.73,0.73,0.73))
        c.drawRightString(W - PAD_X, y - 18, contact_line[:80])

        # Bottom rule in secondary colour (full width inside pad)
        r,g,b = _rgb(t.color_secondary or "#2C2C2A")
        c.setStrokeColor(Color(r/255,g/255,b/255)); c.setLineWidth(1.0)
        c.line(PAD_X, y - 50, W - PAD_X, y - 50)

        # Footer — thin rule + muted centred text
        c.setStrokeColor(Color(0.87,0.87,0.87)); c.setLineWidth(0.6)
        c.line(PAD_X, 28, W - PAD_X, 28)
        c.setFont(font_b, 8); c.setFillColor(Color(0.73,0.73,0.73))
        c.drawCentredString(W/2, 16, f"{t.contact_info or ''}   |   {addr_flat}"[:110])

    # ════════════════════════════════════════════════════════
    # SPLIT — coloured left panel edge-to-edge, white right side
    # ════════════════════════════════════════════════════════
    elif k == "split":
        panel_w = 180
        panel_h = 110

        # Left coloured panel: flush to top AND left edge (no outer margin)
        c.setFillColor(pri)
        c.rect(0, H - panel_h, panel_w, panel_h, stroke=0, fill=1)

        # Left panel content — logo + name (small inner pad)
        ly = H - 18
        if logo_img:
            c.drawImage(logo_img, 14, ly - 34, height=32, width=32,
                        preserveAspectRatio=True, mask="auto")
            ly -= 38

        c.setFont(font_h, 14); c.setFillColor(white)
        cname = (t.company_name or "")[:28]
        c.drawString(14, ly - 14, cname)
        if t.tagline:
            c.setFont(font_bi, 9); c.setFillColor(Color(0.87,0.87,0.87))
            c.drawString(14, ly - 28, t.tagline[:28])

        # Right address block (padded from right edge)
        ay = H - 22
        c.setFont(font_b, 9); c.setFillColor(Color(0.53,0.53,0.53))
        for line in addr_lines:
            c.drawRightString(W - PAD_X, ay, line); ay -= 12
        if t.contact_info:
            c.setFillColor(Color(0.67,0.67,0.67))
            c.drawRightString(W - PAD_X, ay - 2, t.contact_info)

        # Thin footer rule + muted text
        c.setStrokeColor(Color(0.87,0.87,0.87)); c.setLineWidth(0.6)
        c.line(PAD_X, 28, W - PAD_X, 28)
        c.setFont(font_b, 8); c.setFillColor(Color(0.67,0.67,0.67))
        c.drawCentredString(W/2, 16, f"{t.contact_info or ''}  ·  {addr_flat}"[:110])

    # ════════════════════════════════════════════════════════
    # ELEGANT — centred content with double rules (full-width rules)
    # ════════════════════════════════════════════════════════
    elif k == "elegant":
        y = H - 16   # small top pad

        if logo_img:
            c.drawImage(logo_img, W/2 - 24, y - 52, height=48, width=48,
                        preserveAspectRatio=True, mask="auto")
            y -= 56

        c.setFont(font_h, 22); c.setFillColor(sec)
        c.drawCentredString(W/2, y - 20, t.company_name or ""); y -= 22
        if t.tagline:
            c.setFont(font_bi, 11); c.setFillColor(pri)
            c.drawCentredString(W/2, y - 14, t.tagline); y -= 16

        # Double rule (full-width edge-to-edge)
        c.setStrokeColor(pri); c.setLineWidth(2.5)
        c.line(0, y - 20, W, y - 20)
        c.setLineWidth(0.8)
        c.line(0, y - 25, W, y - 25)

        contact_line = addr_flat
        if t.contact_info:
            contact_line = f"{addr_flat}  ·  {t.contact_info}" if addr_flat else t.contact_info
        c.setFont(font_b, 9); c.setFillColor(Color(0.67,0.67,0.67))
        c.drawCentredString(W/2, y - 40, contact_line[:100])

        # Double rule footer (full-width)
        c.setStrokeColor(pri); c.setLineWidth(2.5)
        c.line(0, 36, W, 36)
        c.setLineWidth(0.8)
        c.line(0, 31, W, 31)
        c.setFont(font_b, 8); c.setFillColor(Color(0.67,0.67,0.67))
        c.drawCentredString(W/2, 18, (t.contact_info or "")[:100])

    else:
        # Fallback — simple top bar + name + coloured footer
        bar_h = 4
        c.setFillColor(pri); c.rect(0, H - bar_h, W, bar_h, stroke=0, fill=1)
        y = H - bar_h - 24
        c.setFont(font_h, 18); c.setFillColor(sec)
        c.drawString(PAD_X, y, t.company_name or "")
        c.setStrokeColor(pri); c.setLineWidth(1)
        c.line(PAD_X, y - 10, W - PAD_X, y - 10)
        draw_footer_band()

    c.save()
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# PNG  (Pillow — A4 portrait page at 150 DPI, 1240×1754 px)
# ═══════════════════════════════════════════════════════════════════════════
#
# The PNG export is a full A4 portrait page (not a landscape strip) so it
# can be used as a single-page image letterhead — drop it into any editor
# that supports image backgrounds. The header/footer layout mirrors the
# PDF export: background shapes go edge-to-edge; only text/logos respect
# an inner horizontal pad.

def _build_png(t: LetterheadTemplate) -> bytes:
    _require('PIL', 'Pillow', 'PNG export')
    from PIL import Image, ImageDraw, ImageFont

    # A4 portrait at 150 DPI — 210mm × 297mm
    W, H = 1240, 1754
    img  = Image.new("RGB", (W, H), (255,255,255))
    d    = ImageDraw.Draw(img)

    pri  = _rgb(t.color_primary  or "#1D9E75")
    sec_ = _rgb(t.color_secondary or "#2C2C2A")
    k    = t.template_key or "classic"
    MARG = 80   # inner horizontal pad for text/logo content

    # Font loading — try DejaVu first (common on Ubuntu), then Liberation
    def _font(size, bold=False, italic=False):
        candidates = []
        if bold and italic:
            candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"]
        elif bold:
            candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        elif italic:
            candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"]
        else:
            candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
        for fp in candidates:
            if os.path.exists(fp):
                try: return ImageFont.truetype(fp, size)
                except: pass
        return ImageFont.load_default()

    def tw(text, font):
        try:
            bb = d.textbbox((0,0), text, font=font)
            return bb[2]-bb[0]
        except: return len(text)*7

    fn  = _font(38, bold=True)
    ft  = _font(20, italic=True)
    fb  = _font(18)
    fbs = _font(16)

    # Load logo
    logo_img = None
    lp = _logo_path(t)
    if lp:
        try:
            logo_img = Image.open(lp).convert("RGBA")
            lh = 70
            lw = int(lh * logo_img.width / logo_img.height)
            logo_img = logo_img.resize((lw, lh), Image.LANCZOS)
        except: logo_img = None

    addr_lines = [l.strip() for l in (t.address or "").split("\n") if l.strip()]
    addr_flat  = "  ·  ".join(addr_lines)

    def draw_footer_band(text, bg=None):
        bg = bg or pri
        d.rectangle([0, H-56, W, H], fill=bg)
        d.text(((W - tw(text, fbs))//2, H-38), text[:120], fill=(255,255,255), font=fbs)

    # ════════════════════════════════════════════════════════
    if k == "classic":
        d.rectangle([0, 0, W, 10], fill=pri)  # full-width top bar
        lx = MARG
        if logo_img:
            img.paste(logo_img, (lx, 40), logo_img)
            lx += logo_img.width + 14
        d.text((lx, 40), t.company_name or "", fill=sec_, font=fn)
        if t.tagline:
            d.text((lx, 86), t.tagline, fill=pri, font=ft)
        ay = 44
        for line in addr_lines:
            d.text((W - MARG - tw(line, fbs), ay), line, fill=(136,136,136), font=fbs); ay += 22
        if t.contact_info:
            d.text((W-MARG-tw(t.contact_info, fbs), ay+4), t.contact_info, fill=(170,170,170), font=fbs)
        d.rectangle([MARG, 132, W-MARG, 134], fill=(220,220,220))
        parts = [t.contact_info or "", addr_flat]
        draw_footer_band("  ·  ".join([p for p in parts if p]))

    elif k == "modern":
        d.rectangle([0, 0, 8, 130], fill=pri)  # flush-left accent stripe
        lx = MARG + 22
        if logo_img:
            img.paste(logo_img, (lx, 36), logo_img)
            lx += logo_img.width + 14
        d.text((lx, 40), t.company_name or "", fill=sec_, font=fn)
        if t.tagline:
            d.text((lx, 86), t.tagline, fill=(170,170,170), font=ft)
        ay = 44
        if t.contact_info:
            d.text((W-MARG-tw(t.contact_info, fbs), ay), t.contact_info, fill=(136,136,136), font=fbs); ay += 22
        for line in addr_lines:
            d.text((W-MARG-tw(line, fbs), ay), line, fill=(187,187,187), font=fbs); ay += 22
        d.rectangle([MARG+12, 134, W-MARG, 136], fill=(220,220,220))
        parts = [t.contact_info or "", addr_flat]
        d.rectangle([0, H-40, W, H], fill=(255,255,255))
        d.rectangle([0, H-40, W, H-38], fill=pri)
        text = "  ·  ".join([p for p in parts if p])
        d.text(((W - tw(text, fbs))//2, H-28), text[:120], fill=pri, font=fbs)

    elif k == "bold":
        banner_h = 160 + (30 if t.tagline else 0)
        d.rectangle([0, 0, W, banner_h], fill=sec_)
        lx = MARG
        if logo_img:
            img.paste(logo_img, (lx, 40), logo_img)
            lx += logo_img.width + 16
        d.text((lx, 44), t.company_name or "", fill=(255,255,255), font=fn)
        ty = 96
        if t.tagline:
            d.text((lx, ty), t.tagline, fill=(170,170,170), font=ft); ty += 34
        if t.contact_info:
            d.text((lx, ty), t.contact_info, fill=(153,153,153), font=fbs)
        d.rectangle([0, banner_h, W, banner_h+8], fill=pri)
        footer_text = f"{t.company_name or ''}  ·  {addr_flat}  ·  {t.contact_info or ''}"
        draw_footer_band(footer_text, bg=sec_)
        d.rectangle([0, H-56, W, H-53], fill=pri)  # accent line above footer

    elif k == "minimal":
        lx = MARG
        if logo_img:
            img.paste(logo_img, (lx, 34), logo_img)
            lx += logo_img.width + 14
        d.text((lx, 38), t.company_name or "", fill=sec_, font=fn)
        if t.tagline:
            tag_x = lx + tw(t.company_name or "", fn) + 20
            d.text((tag_x, 52), f"· {t.tagline}", fill=(187,187,187), font=ft)
        contact_line = addr_flat
        if t.contact_info: contact_line = f"{addr_flat}  ·  {t.contact_info}" if addr_flat else t.contact_info
        d.text((W-MARG-tw(contact_line, fbs), 48), contact_line[:80], fill=(187,187,187), font=fbs)
        d.rectangle([MARG, 118, W-MARG, 120], fill=sec_)
        d.rectangle([MARG, H-36, W-MARG, H-35], fill=(220,220,220))
        ftr = f"{t.contact_info or ''}   |   {addr_flat}"
        d.text(((W - tw(ftr, fbs))//2, H-28), ftr[:120], fill=(187,187,187), font=fbs)

    elif k == "split":
        panel_w = 300
        panel_h = 220   # fixed height — not tied to page height anymore
        d.rectangle([0, 0, panel_w, panel_h], fill=pri)
        ly = 26
        if logo_img:
            img.paste(logo_img, (20, ly), logo_img)
            ly += logo_img.height + 10
        name_lines = [t.company_name[i:i+18] for i in range(0, len(t.company_name or ""), 18)]
        for nl in name_lines[:2]:
            d.text((20, ly), nl, fill=(255,255,255), font=_font(26, bold=True)); ly += 34
        if t.tagline:
            d.text((20, ly+4), t.tagline[:22], fill=(221,221,221), font=_font(17, italic=True))
        ay = 40
        for line in addr_lines:
            d.text((W-MARG-tw(line, fbs), ay), line, fill=(136,136,136), font=fbs); ay += 22
        if t.contact_info:
            d.text((W-MARG-tw(t.contact_info, fbs), ay+4), t.contact_info, fill=(170,170,170), font=fbs)
        d.rectangle([0, H-56, W, H], fill=(240,240,240))
        ftr = f"{t.contact_info or ''}  ·  {addr_flat}"
        d.text(((W - tw(ftr, fbs))//2, H-38), ftr[:120], fill=(170,170,170), font=fbs)

    elif k == "elegant":
        y = 30
        if logo_img:
            img.paste(logo_img, ((W-logo_img.width)//2, y), logo_img)
            y += logo_img.height + 14
        name_text = t.company_name or ""
        d.text(((W - tw(name_text, fn))//2, y), name_text, fill=sec_, font=fn); y += 50
        if t.tagline:
            d.text(((W - tw(t.tagline, ft))//2, y), t.tagline, fill=pri, font=ft); y += 32
        # Double rule (full width)
        d.rectangle([0, y, W, y+4], fill=pri); y += 10
        d.rectangle([0, y, W, y+2], fill=pri); y += 12
        contact_line = addr_flat
        if t.contact_info: contact_line = f"{addr_flat}  ·  {t.contact_info}" if addr_flat else t.contact_info
        d.text(((W - tw(contact_line, fbs))//2, y), contact_line[:100], fill=(170,170,170), font=fbs)
        # Double rule footer (full width)
        d.rectangle([0, H-58, W, H-54], fill=pri)
        d.rectangle([0, H-50, W, H-48], fill=pri)
        ftr = t.contact_info or ""
        d.text(((W - tw(ftr, fbs))//2, H-38), ftr[:100], fill=(170,170,170), font=fbs)

    else:
        d.text((MARG, 40), t.company_name or "", fill=sec_, font=fn)
        d.rectangle([MARG, 90, W-MARG, 93], fill=pri)
        draw_footer_band(t.contact_info or "")

    out = io.BytesIO()
    img.save(out, format="PNG", dpi=(150, 150))
    return out.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# DJANGO VIEWS
# ═══════════════════════════════════════════════════════════════════════════

class LetterheadDocxExportView(LoginRequiredMixin, View):
    """GET /letterhead/api/templates/<uuid>/export/docx/"""
    def get(self, request, pk):
        template = get_object_or_404(LetterheadTemplate, pk=pk)
        try:
            data = _build_docx(template)
        except MissingDependencyError as exc:
            logger.error("DOCX dependency missing: %s", exc)
            return JsonResponse({"error": str(exc)}, status=501)
        except Exception as exc:
            logger.exception("DOCX export failed for %s", pk)
            return JsonResponse({"error": str(exc)}, status=500)
        safe = template.name.replace(" ","_").replace("/","-")
        r = HttpResponse(data,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        r["Content-Disposition"] = f'attachment; filename="letterhead_{safe}.docx"'
        r["Content-Length"] = len(data)
        return r


class LetterheadPdfExportView(LoginRequiredMixin, View):
    """GET /letterhead/api/templates/<uuid>/export/pdf/"""
    def get(self, request, pk):
        template = get_object_or_404(LetterheadTemplate, pk=pk)
        try:
            data = _build_pdf(template)
        except MissingDependencyError as exc:
            logger.error("PDF dependency missing: %s", exc)
            return JsonResponse({"error": str(exc)}, status=501)
        except Exception as exc:
            logger.exception("PDF export failed for %s", pk)
            return JsonResponse({"error": str(exc)}, status=500)
        safe = template.name.replace(" ","_").replace("/","-")
        r = HttpResponse(data, content_type="application/pdf")
        r["Content-Disposition"] = f'attachment; filename="letterhead_{safe}.pdf"'
        r["Content-Length"] = len(data)
        return r


class LetterheadPngExportView(LoginRequiredMixin, View):
    """GET /letterhead/api/templates/<uuid>/export/png/"""
    def get(self, request, pk):
        template = get_object_or_404(LetterheadTemplate, pk=pk)
        try:
            data = _build_png(template)
        except MissingDependencyError as exc:
            logger.error("PNG dependency missing: %s", exc)
            return JsonResponse({"error": str(exc)}, status=501)
        except Exception as exc:
            logger.exception("PNG export failed for %s", pk)
            return JsonResponse({"error": str(exc)}, status=500)
        safe = template.name.replace(" ","_").replace("/","-")
        r = HttpResponse(data, content_type="image/png")
        r["Content-Disposition"] = f'attachment; filename="letterhead_{safe}.png"'
        r["Content-Length"] = len(data)
        return r


# Keep this for backward-compat if old urls.py still references it
LetterheadDocxExportView = LetterheadDocxExportView