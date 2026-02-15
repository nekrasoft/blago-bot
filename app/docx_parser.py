from __future__ import annotations

from pathlib import Path

from docx import Document



def _clean_line(line: str) -> str:
    return " ".join(line.replace("\xa0", " ").split()).strip()



def extract_docx_text(file_path: Path) -> str:
    doc = Document(str(file_path))
    blocks: list[str] = []

    for paragraph in doc.paragraphs:
        text = _clean_line(paragraph.text)
        if text:
            blocks.append(text)

    for table in doc.tables:
        for row in table.rows:
            cells = [_clean_line(cell.text) for cell in row.cells]
            cells = [cell for cell in cells if cell]
            if cells:
                blocks.append(" | ".join(cells))

    return "\n".join(blocks)
