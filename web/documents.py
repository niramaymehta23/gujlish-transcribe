"""Word (.docx) exports of transcripts, translations and summaries."""

import re
from io import BytesIO


def _new_document(title: str):
    # Keep the Python-only server small; standard Word paragraphs preserve Unicode.
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.27), Inches(11.69)
    section.top_margin = section.bottom_margin = Inches(0.8)
    normal = document.styles["Normal"]
    normal.font.name, normal.font.size = "Nirmala UI", Pt(11)
    normal.paragraph_format.line_spacing = 1.4
    fonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:cs"), "Nirmala UI")
    document.core_properties.title = title
    return document


def _saved(document) -> BytesIO:
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def transcript_docx(text: str, title: str) -> BytesIO:
    document = _new_document(title)
    for paragraph in text.splitlines() or [""]:
        document.add_paragraph(paragraph)
    return _saved(document)


def markdown_docx(markdown: str, title: str) -> BytesIO:
    """Render the summary's small Markdown subset (headings, bullets, quotes, bold/italic) as Word."""

    def plain(text: str) -> str:
        return re.sub(r"\*(.+?)\*", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", text))

    document = _new_document(title)
    for line in markdown.splitlines():
        if line.startswith("### "):
            document.add_heading(plain(line[4:]), level=3)
        elif line.startswith("## "):
            document.add_heading(plain(line[3:]), level=2)
        elif line.startswith("# "):
            document.add_heading(plain(line[2:]), level=1)
        elif line.startswith("- "):
            document.add_paragraph(plain(line[2:]), style="List Bullet")
        elif line.startswith("> "):
            document.add_paragraph().add_run(plain(line[2:])).italic = True
        elif line.strip():
            document.add_paragraph(plain(line))
    return _saved(document)
