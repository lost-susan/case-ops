"""Human-readable source references used everywhere an insight points at its proof."""
from app.models.orm import DocLine, Document


def locator(doc: Document, line: DocLine) -> str:
    if doc.kind in ("wav", "mp3"):
        sec = int(line.start_sec or 0)
        return f"{sec // 60:02d}:{sec % 60:02d}"
    if doc.kind == "txt":
        return f"line {line.idx + 1}"
    return f"p. {line.page}"


def source_ref(doc: Document, line: DocLine) -> dict:
    return {
        "line_id": line.id, "document_id": doc.id, "filename": doc.filename,
        "locator": locator(doc, line), "quote": line.text,
    }
