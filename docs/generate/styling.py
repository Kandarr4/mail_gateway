"""Оформление документов Word: шрифты, заголовки, таблицы, врезки.

Вынесено отдельно, чтобы оба документа выглядели как один комплект, а правка
оформления не требовала обхода обоих сценариев.
"""

from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BODY_FONT = "Calibri"
CODE_FONT = "Consolas"

INK = RGBColor(0x1C, 0x21, 0x26)
MUTED = RGBColor(0x60, 0x6A, 0x76)
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
WARN = RGBColor(0x9C, 0x27, 0x11)

SHADE_HEADER = "1F4E79"
SHADE_ROW = "F2F5F8"
SHADE_NOTE = "FFF6E5"
SHADE_CODE = "F4F5F7"


def new_document():
    from docx import Document

    document = Document()
    _setup_base_style(document)
    _setup_page(document)
    return document


def _setup_base_style(document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(11)
    normal.font.color.rgb = INK
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    # Word хранит шрифт для кириллицы отдельным атрибутом: без него текст
    # набирается запасным шрифтом и выглядит чужеродно.
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:cs"), BODY_FONT)
    rfonts.set(qn("w:eastAsia"), BODY_FONT)


def _setup_page(document) -> None:
    for section in document.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.2)
        section.right_margin = Cm(1.8)


# --- Текстовые блоки ----------------------------------------------------------- #


def title(document, text: str, subtitle: str = "") -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(2)
    run = paragraph.add_run(text)
    run.font.size = Pt(26)
    run.font.bold = True
    run.font.color.rgb = ACCENT

    if subtitle:
        sub = document.add_paragraph()
        sub.paragraph_format.space_after = Pt(14)
        run = sub.add_run(subtitle)
        run.font.size = Pt(12)
        run.font.color.rgb = MUTED


def heading(document, text: str, level: int = 1) -> None:
    sizes = {1: 16, 2: 13, 3: 11.5}
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(16 if level == 1 else 12)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(text)
    run.font.size = Pt(sizes.get(level, 11))
    run.font.bold = True
    run.font.color.rgb = ACCENT if level == 1 else INK


def para(document, text: str = "", *, bold=False, muted=False, size=11, align=None):
    paragraph = document.add_paragraph()
    if align == "center":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif align == "right":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run(text)
    run.font.bold = bold
    run.font.size = Pt(size)
    if muted:
        run.font.color.rgb = MUTED
    return paragraph


def rich(document, parts: list[tuple[str, str]]):
    """Абзац из кусков: [('обычный ', ''), ('жирный', 'b'), ('код', 'c')]."""
    paragraph = document.add_paragraph()
    for text, kind in parts:
        run = paragraph.add_run(text)
        if "b" in kind:
            run.font.bold = True
        if "i" in kind:
            run.font.italic = True
        if "c" in kind:
            run.font.name = CODE_FONT
            run.font.size = Pt(10)
        if "m" in kind:
            run.font.color.rgb = MUTED
    return paragraph


def bullets(document, items: list, numbered: bool = False) -> None:
    style = "List Number" if numbered else "List Bullet"
    for item in items:
        paragraph = document.add_paragraph(style=style)
        paragraph.paragraph_format.space_after = Pt(3)
        if isinstance(item, tuple):
            head, tail = item
            run = paragraph.add_run(head)
            run.font.bold = True
            paragraph.add_run(tail)
        else:
            paragraph.add_run(item)


def code(document, lines: str) -> None:
    """Моноширинный блок на сером фоне."""
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.left_indent = Cm(0.4)
    run = paragraph.add_run(lines)
    run.font.name = CODE_FONT
    run.font.size = Pt(9.5)
    _shade_paragraph(paragraph, SHADE_CODE)


def note(document, text: str, *, warning: bool = False) -> None:
    """Врезка «обратите внимание»."""
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    cell = table.cell(0, 0)
    cell.text = ""
    paragraph = cell.paragraphs[0]
    run = paragraph.add_run(text)
    run.font.size = Pt(10)
    if warning:
        run.font.color.rgb = WARN
        run.font.bold = True
    _shade_cell(cell, SHADE_NOTE)
    _set_borders(table, color="E8C97A")
    document.add_paragraph().paragraph_format.space_after = Pt(2)


def table(document, headers: list[str], rows: list[list[str]], widths: list[float] | None = None):
    grid = document.add_table(rows=1, cols=len(headers))
    grid.alignment = WD_TABLE_ALIGNMENT.LEFT

    for index, caption in enumerate(headers):
        cell = grid.rows[0].cells[index]
        cell.text = ""
        run = cell.paragraphs[0].add_run(caption)
        run.font.bold = True
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _shade_cell(cell, SHADE_HEADER)

    for position, row in enumerate(rows):
        cells = grid.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = ""
            paragraph = cells[index].paragraphs[0]
            # Ведущая звёздочка помечает строку как выделенную (итог).
            emphasise = str(value).startswith("*")
            run = paragraph.add_run(str(value).lstrip("*"))
            run.font.size = Pt(10)
            run.font.bold = emphasise
            if position % 2 == 1:
                _shade_cell(cells[index], SHADE_ROW)

    if widths:
        for row in grid.rows:
            for index, width in enumerate(widths):
                row.cells[index].width = Cm(width)

    _set_borders(grid)
    document.add_paragraph().paragraph_format.space_after = Pt(2)
    return grid


def page_break(document) -> None:
    from docx.enum.text import WD_BREAK

    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def footer_note(document, text: str) -> None:
    for section in document.sections:
        paragraph = section.footer.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        run.font.size = Pt(8)
        run.font.color.rgb = MUTED


# --- Низкоуровневое оформление -------------------------------------------------- #


def _shade_cell(cell, color: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(shading)


def _shade_paragraph(paragraph, color: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:fill"), color)
    paragraph._p.get_or_add_pPr().append(shading)


def _set_borders(grid, color: str = "D5DBE1") -> None:
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:color"), color)
        borders.append(element)
    grid._tbl.tblPr.append(borders)
