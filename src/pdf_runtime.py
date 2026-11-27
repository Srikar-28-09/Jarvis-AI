"""Small helpers for optional PDF runtime dependencies."""

PDF_VIEWER_PYMUPDF_MISSING = (
    "PDF viewing and document export require PyMuPDF. Install optional PDF dependencies with "
    "`pip install -r requirements-optional.txt` (PyMuPDF is AGPL-3.0)."
)


def load_pymupdf_for_pdf_viewer():
    """Return PyMuPDF, or raise a user-facing setup hint."""
    try:
        import fitz  # PyMuPDF, optional
    except ImportError as exc:
        raise RuntimeError(PDF_VIEWER_PYMUPDF_MISSING) from exc
    return fitz


def write_text_document_pdf(path, title: str, content: str) -> None:
    """Render a plain-text document into a downloadable PDF using PyMuPDF."""
    from html import escape

    fitz = load_pymupdf_for_pdf_viewer()
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{font-family:sans-serif;font-size:11pt;line-height:1.45;color:#111;}"
        "h1{font-size:20pt;margin:0 0 18pt;}pre{font-family:sans-serif;"
        "font-size:10.5pt;white-space:pre-wrap;overflow-wrap:anywhere;}"
        "</style></head><body>"
        f"<h1>{escape(title or 'Document')}</h1>"
        f"<pre>{escape(content or '')}</pre>"
        "</body></html>"
    )
    story = fitz.Story(html=html)
    writer = fitz.DocumentWriter(str(path))
    page_rect = fitz.paper_rect("a4")
    content_rect = page_rect + (48, 48, -48, -48)
    try:
        more = True
        while more:
            page = writer.begin_page(page_rect)
            more, _filled = story.place(content_rect)
            story.draw(page)
            writer.end_page()
    finally:
        writer.close()
