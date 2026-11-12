"""Builds the Clean Base Template (docxtpl/Jinja tags) from scratch.

Why from scratch instead of cleaning a copy of the supplied 78 MB document: nothing from the
original (text, images, links, rsids, metadata) can leak, and the result is reproducible.
Visual conventions of the original are kept: US Letter, 1" margins, Calibri body, heading sizes
20/16/14/12 pt, table header fill #1F5C4C, a footer PAGE field, a TOC field.

Run:  python -m app.srs_engine.template_builder
"""
import hashlib
from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

BRAND = "1F5C4C"
TEMPLATE_PATH = Path("templates/srs_base_template.docx")
MANIFEST_PATH = Path("templates/srs_base_template.manifest.yaml")
DENYLIST_PATH = Path("templates/leakage_denylist.txt")

DENYLIST = """# Strings that must never appear in the template or in any generated SRS.
# One per line, matched case-insensitively. They come from the supplied sample document.
DLAS
DBLA
UNDP
European Union
Legal Aid
Ministry of Law
bdlaws.minlaw.gov.bd
dlas-demo.oss.net.bd
figma.com
Keycloak
16699
"""

MANIFEST = """# Manifest of the Clean Base Template (versioned with the template).
template: srs_base_template.docx
template_version: 1
renderer: docxtpl
sections:
  - {key: document_control, heading: Document Control, method: D, mode: core}
  - {key: revision_history, heading: Revision History, method: D, mode: core}
  - {key: introduction, heading: Introduction, method: L, mode: core}
  - {key: overall_description, heading: Overall Description, method: L, mode: core}
  - {key: functional_requirements, heading: Functional Requirements, method: D, mode: core}
  - {key: non_functional_requirements, heading: Non-Functional Requirements, method: D, mode: cond}
  - {key: security_requirements, heading: Security Requirements, method: D, mode: cond}
  - {key: business_rules, heading: Business Rules, method: D, mode: cond}
  - {key: integrations, heading: Integration Requirements, method: D, mode: cond}
  - {key: other_requirements, heading: Constraints, Assumptions and Dependencies, method: D, mode: cond}
  - {key: gaps, heading: Gap and Ambiguity Analysis, method: D, mode: core}
status_labels:
  confirmed: CONFIRMED
  proposed: PROPOSED
  modified: MODIFIED — PENDING CONFIRMATION
  needs_clarification: NEEDS CLARIFICATION
"""


def _shade(cell, color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color)
    tc_pr.append(shd)


def _field(paragraph, instruction: str, placeholder: str = "") -> None:
    def fld(kind: str):
        run = paragraph.add_run()
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), kind)
        run._r.append(el)

    fld("begin")
    run = paragraph.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    run._r.append(instr)
    fld("separate")
    paragraph.add_run(placeholder)
    fld("end")


def _para(doc, text: str = "", label: str | None = None, style: str | None = None):
    p = doc.add_paragraph(style=style)
    if label:
        p.add_run(label).bold = True
    if text:
        p.add_run(text)
    return p


def _table(doc, headers: list[str], loop: str, cells: list[str], widths: list[float] | None = None):
    """A header row, a `{%tr for %}` row, a data row and a `{%tr endfor %}` row."""
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, headers):
        _shade(cell, BRAND)
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    start = table.add_row().cells
    start[0].text = "{%tr for r in " + loop + " %}"
    row = table.add_row().cells
    for cell, expr in zip(row, cells):
        cell.text = "{{ " + expr + " }}"
    end = table.add_row().cells
    end[0].text = "{%tr endfor %}"
    if widths:
        for r in table.rows:
            for cell, w in zip(r.cells, widths):
                cell.width = Inches(w)
    return table


def _styles(doc) -> None:
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    for name, size, color in (("Heading 1", 20, BRAND), ("Heading 2", 16, BRAND),
                              ("Heading 3", 14, "434343"), ("Heading 4", 12, "666666")):
        style = doc.styles[name]
        style.font.name, style.font.size = "Calibri", Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.font.bold = True


def _section(doc) -> None:
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Inches(1)
    footer = sec.footer.paragraphs[0]
    footer.text = "{{ project.title }} — Software Requirements Specification v{{ doc.version }}    Page "
    _field(footer, "PAGE", "1")


def _settings(doc) -> None:
    settings = doc.settings.element
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    settings.append(update)
    core = doc.core_properties
    core.title, core.author, core.comments, core.subject = (
        "Software Requirements Specification", "", "", "")
    core.last_modified_by = ""


def _front_matter(doc) -> None:
    title = doc.add_paragraph()
    run = title.add_run("{{ project.title }}")
    run.bold, run.font.size = True, Pt(28)
    run.font.color.rgb = RGBColor.from_string(BRAND)
    sub = doc.add_paragraph()
    sub.add_run("Software Requirements Specification").font.size = Pt(18)
    _para(doc, "{{ project.subtitle }}")
    _para(doc, "{{ project.client }}   |   Reference {{ project.token }}")
    doc.add_heading("Document Control", level=2)
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    rows = (("Title", "{{ project.title }}"), ("Project", "{{ project.code }}"),
            ("Client", "{{ project.client }}"), ("Version", "{{ doc.version }}"),
            ("Status", "{{ doc.status }}"), ("Date", "{{ doc.date }}"),
            ("Prepared By", "{{ doc.prepared_by }}"), ("Reviewers", "{{ doc.reviewers }}"),
            ("Approvers", "{{ doc.approvers }}"))
    for key, value in rows:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = key, value
        _shade(cells[0], "E8F0EC")
        cells[0].paragraphs[0].runs[0].bold = True
    _para(doc, "{%p if final_approval %}")
    _para(doc, "{{ final_approval.text }}", label="Approval: ")
    _para(doc, "{%p endif %}")
    doc.add_heading("Revision History", level=2)
    _table(doc, ["Version", "Date", "Author", "Description"], "revisions",
           ["r.version", "r.date", "r.author", "r.description"], [0.9, 1.1, 1.4, 3.1])
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    doc.add_heading("Table of Contents", level=1)
    _field(doc.add_paragraph(), 'TOC \\o "1-3" \\h \\z \\u', "Right-click and choose Update Field.")
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _fr_block(doc) -> None:
    doc.add_heading("3. Functional Requirements", level=1)
    _para(doc, "{%p if not frs %}")
    _para(doc, "No functional requirements have been captured yet.")
    _para(doc, "{%p endif %}")
    _para(doc, "{%p for fr in frs %}")
    doc.add_heading("{{ fr.code }} — {{ fr.title }}", level=3)
    status = _para(doc, "{{ fr.status_label }}")
    status.runs[0].bold, status.runs[0].font.size = True, Pt(8)
    status.runs[0].font.color.rgb = RGBColor.from_string(BRAND)
    _para(doc, "{{ fr.statement }}", label="Requirement: ")
    for key, label in (("purpose", "Purpose: "), ("actors", "Actor: "), ("trigger", "Trigger: "),
                       ("preconditions", "Preconditions: "), ("rationale", "Rationale: ")):
        _para(doc, "{%p if fr." + key + " %}")
        _para(doc, "{{ fr." + key + " }}", label=label)
        _para(doc, "{%p endif %}")
    _para(doc, "{%p if fr.main_flow %}")
    _para(doc, "", label="Main Flow:")
    _para(doc, "{%p for step in fr.main_flow %}")
    _para(doc, "{{ loop.index }}. {{ step }}")
    _para(doc, "{%p endfor %}")
    _para(doc, "{%p endif %}")
    _para(doc, "{%p if fr.acceptance %}")
    _para(doc, "", label="Acceptance Criteria:")
    _para(doc, "{%p for ac in fr.acceptance %}")
    _para(doc, "Given {{ ac.given }}, when {{ ac.when }}, then {{ ac.then }}.")
    _para(doc, "{%p endfor %}")
    _para(doc, "{%p endif %}")
    _para(doc, "{{ fr.priority }}", label="Priority: ")
    _para(doc, "{{ fr.source_note }}", label="Source: ")
    _para(doc, "{%p endfor %}")


def build_template(path: Path = TEMPLATE_PATH) -> str:
    doc = Document()
    _styles(doc)
    _section(doc)
    _settings(doc)
    _front_matter(doc)
    doc.add_heading("1. Introduction", level=1)
    doc.add_heading("1.1 Purpose", level=2)
    _para(doc, "{{ narrative.purpose }}")
    doc.add_heading("1.2 Scope", level=2)
    _para(doc, "{{ narrative.scope }}")
    doc.add_heading("2. Overall Description", level=1)
    doc.add_heading("2.1 Executive Summary", level=2)
    _para(doc, "{{ narrative.executive_summary }}")
    doc.add_heading("2.2 Business Context", level=2)
    _para(doc, "{{ narrative.business_context }}")
    _fr_block(doc)
    cols = ["Code", "Requirement", "Priority", "Status"]
    exprs = ["r.code", "r.title ~ ': ' ~ r.statement", "r.priority", "r.status_label"]
    widths = [0.9, 3.9, 0.8, 1.0]
    for number, (title, var, key) in enumerate((
            ("Non-Functional Requirements", "nfrs", "nfrs"), ("Security Requirements", "secs", "secs"),
            ("Business Rules", "rules", "rules"), ("Integration Requirements", "ints", "ints")), start=4):
        _para(doc, "{%p if " + key + " %}")
        doc.add_heading(f"{number}. {title}", level=1)
        _table(doc, cols, var, exprs, widths)
        _para(doc, "{%p endif %}")
    _para(doc, "{%p if others %}")
    doc.add_heading("8. Constraints, Assumptions and Dependencies", level=1)
    _table(doc, ["Code", "Type", "Statement", "Status"], "others",
           ["r.code", "r.type", "r.title ~ ': ' ~ r.statement", "r.status_label"], [0.9, 0.9, 3.8, 1.0])
    _para(doc, "{%p endif %}")
    doc.add_heading("9. Gap and Ambiguity Analysis", level=1)
    _para(doc, "{%p if not issues %}")
    _para(doc, "No open gaps or ambiguities were identified.")
    _para(doc, "{%p endif %}")
    _para(doc, "{%p if issues %}")
    _table(doc, ["Kind", "Item", "Detail", "Blocking"], "issues",
           ["r.kind", "r.title", "r.description", "r.blocking"], [1.2, 2.0, 2.8, 0.6])
    _para(doc, "{%p endif %}")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_support_files() -> None:
    MANIFEST_PATH.write_text(MANIFEST, encoding="utf-8")
    DENYLIST_PATH.write_text(DENYLIST, encoding="utf-8")


def main() -> None:
    digest = build_template()
    write_support_files()
    print(f"template built: {TEMPLATE_PATH} sha256={digest}")


if __name__ == "__main__":
    main()
