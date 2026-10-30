"""Word document extraction worker."""


def extract(file_path: str) -> dict:
    import docx
    doc = docx.Document(file_path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    # also extract tables
    table_lines = []
    for table in doc.tables:
        for row in table.rows:
            table_lines.append("\t".join(cell.text.strip() for cell in row.cells))
    text = "\n".join(paragraphs)
    if table_lines:
        text += "\n\n=== Tables ===\n" + "\n".join(table_lines)
    return {"text": text, "pages": len(doc.sections), "method": "python-docx"}
