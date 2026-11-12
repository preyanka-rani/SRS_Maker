"""PDF export. Gotenberg (LibreOffice) gives a faithful rendering of the DOCX; when it is not
configured, a complete PDF is generated from the same context with reportlab."""
import hashlib
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pypdfium2 as pdfium
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import Settings
from app.srs_engine.context_builder import SrsContext

BRAND = colors.HexColor("#1F5C4C")


def _via_gotenberg(settings: Settings, docx_path: Path, pdf_path: Path) -> None:
    url = settings.gotenberg_url.rstrip("/") + "/forms/libreoffice/convert"
    with open(docx_path, "rb") as fh:
        response = httpx.post(url, files={"files": (docx_path.name, fh)}, timeout=180)
    response.raise_for_status()
    pdf_path.write_bytes(response.content)


def _p(text: object, style) -> Paragraph:
    return Paragraph(escape(str(text or "")), style)


def _table(rows: list[list[str]], widths: list[float], body, head) -> Table:
    data = [[_p(c, head if i == 0 else body) for c in row] for i, row in enumerate(rows)]
    table = Table(data, colWidths=[w * inch for w in widths], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), BRAND),
                               ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return table


def _fr_story(fr: dict, body, h3) -> list:
    out = [_p(f"{fr['code']} — {fr['title']}", h3), _p(fr["status_label"], body),
           _p("Requirement: " + fr["statement"], body)]
    for key, label in (("purpose", "Purpose"), ("actors", "Actor"), ("trigger", "Trigger"),
                       ("preconditions", "Preconditions")):
        if fr[key]:
            out.append(_p(f"{label}: {fr[key]}", body))
    out += [_p(f"{i}. {s}", body) for i, s in enumerate(fr["main_flow"], 1)]
    out += [_p(f"Given {a['given']}, when {a['when']}, then {a['then']}.", body)
            for a in fr["acceptance"]]
    out += [_p(f"Priority: {fr['priority']}", body), _p("Source: " + fr["source_note"], body),
            Spacer(1, 8)]
    return out


def _via_reportlab(context: SrsContext, pdf_path: Path) -> None:
    d = context.data
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=9.5, leading=12)
    head = ParagraphStyle("hd", parent=body, textColor=colors.white, fontName="Helvetica-Bold")
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=BRAND, fontSize=18)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], textColor=colors.HexColor("#434343"))
    story: list = [_p(d["project"]["title"], ParagraphStyle("t", parent=h1, fontSize=26)),
                   _p("Software Requirements Specification", styles["Heading2"]),
                   _p(f"{d['project']['client']}  |  Reference {d['project']['token']}", body),
                   Spacer(1, 12), _p("Document Control", h1)]
    ctl = [["Field", "Value"]] + [[k, str(v)] for k, v in (
        ("Version", d["doc"]["version"]), ("Status", d["doc"]["status"]), ("Date", d["doc"]["date"]),
        ("Prepared By", d["doc"]["prepared_by"]), ("Approvers", d["doc"]["approvers"]))]
    rev = [["Version", "Date", "Author", "Description"]] + [
        [r["version"], r["date"], r["author"], r["description"]] for r in d["revisions"]]
    story += [_table(ctl, [1.6, 4.9], body, head), Spacer(1, 10), _p("Revision History", h1),
              _table(rev, [0.9, 1.1, 1.4, 3.1], body, head), PageBreak()]
    n = d["narrative"]
    for title, text in (("1. Introduction", n["purpose"] + "\n" + n["scope"]),
                        ("2. Overall Description", n["executive_summary"] + "\n" + n["business_context"])):
        story.append(_p(title, h1))
        story += [_p(t, body) for t in text.split("\n") if t.strip()]
    story.append(_p("3. Functional Requirements", h1))
    for fr in d["frs"]:
        story += _fr_story(fr, body, h3)
    for title, key in (("4. Non-Functional Requirements", "nfrs"), ("5. Security Requirements", "secs"),
                       ("6. Business Rules", "rules"), ("7. Integration Requirements", "ints")):
        if d[key]:
            rows = [["Code", "Requirement", "Priority", "Status"]] + [
                [r["code"], f"{r['title']}: {r['statement']}", r["priority"], r["status_label"]]
                for r in d[key]]
            story += [_p(title, h1), _table(rows, [0.9, 3.9, 0.8, 1.0], body, head)]
    if d["others"]:
        rows = [["Code", "Type", "Statement", "Status"]] + [
            [r["code"], r["type"], f"{r['title']}: {r['statement']}", r["status_label"]]
            for r in d["others"]]
        story += [_p("8. Constraints, Assumptions and Dependencies", h1),
                  _table(rows, [0.9, 0.9, 3.8, 1.0], body, head)]
    story.append(_p("9. Gap and Ambiguity Analysis", h1))
    if d["issues"]:
        rows = [["Kind", "Item", "Detail", "Blocking"]] + [
            [i["kind"], i["title"], i["description"], i["blocking"]] for i in d["issues"]]
        story.append(_table(rows, [1.2, 2.0, 2.8, 0.6], body, head))
    else:
        story.append(_p("No open gaps or ambiguities were identified.", body))
    SimpleDocTemplate(str(pdf_path), pagesize=letter, leftMargin=inch, rightMargin=inch,
                      topMargin=inch, bottomMargin=inch,
                      title="Software Requirements Specification").build(story)


def export_pdf(settings: Settings, docx_path: Path, context: SrsContext, pdf_path: Path
               ) -> tuple[str, int]:
    """Returns (sha256, page_count)."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if settings.gotenberg_url:
        _via_gotenberg(settings, docx_path, pdf_path)
    else:
        _via_reportlab(context, pdf_path)
    pages = len(pdfium.PdfDocument(str(pdf_path)))
    return hashlib.sha256(pdf_path.read_bytes()).hexdigest(), pages
