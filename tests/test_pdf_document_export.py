import asyncio
import json
from types import SimpleNamespace

import pytest

from routes.document import document_routes
from src.pdf_runtime import write_text_document_pdf
from src.tool_schemas import function_call_to_tool_block


def test_create_document_tool_arguments_preserve_explicit_pdf_request():
    block = function_call_to_tool_block(
        "create_document",
        json.dumps({
            "title": "Photosynthesis",
            "language": "markdown",
            "content": "Assignment body",
            "export_pdf": True,
        }),
    )

    assert block is not None
    arguments = json.loads(block.content)
    assert arguments == {
        "title": "Photosynthesis",
        "language": "markdown",
        "content": "Assignment body",
        "export_pdf": True,
    }


def test_text_document_pdf_contains_title_and_complete_content(tmp_path):
    fitz = pytest.importorskip("fitz")
    output = tmp_path / "photosynthesis.pdf"
    content = "Introduction\n\nPhotosynthesis converts light energy into chemical energy."

    write_text_document_pdf(output, "Photosynthesis", content)

    with fitz.open(output) as document:
        text = "\n".join(page.get_text() for page in document)
    assert "Photosynthesis" in text
    assert "converts light energy into chemical energy" in text


def test_text_document_pdf_paginates_long_content(tmp_path):
    fitz = pytest.importorskip("fitz")
    output = tmp_path / "long-assignment.pdf"
    content = "\n".join(f"Section {index}: photosynthesis details." for index in range(500))

    write_text_document_pdf(output, "Long assignment", content)

    with fitz.open(output) as document:
        assert document.page_count > 1
        assert "Section 499" in "\n".join(page.get_text() for page in document)


def test_document_export_route_generates_pdf_for_editor_documents(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = SimpleNamespace(
        id="doc-1",
        title="Photosynthesis",
        current_content="The process converts light into chemical energy.",
        owner="alice",
    )

    class Query:
        def filter(self, *_clauses):
            return self

        def first(self):
            return doc

    class Database:
        def query(self, *_models):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr(document_routes, "SessionLocal", Database)
    monkeypatch.setattr(document_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(document_routes, "_verify_doc_owner", lambda *_args: None)
    router = document_routes.setup_document_routes(None)
    route = next(
        route for route in router.routes
        if route.path == "/api/document/{doc_id}/export-pdf"
    )

    response = asyncio.run(route.endpoint(doc_id="doc-1", request=object()))
    try:
        assert response.media_type == "application/pdf"
        with fitz.open(response.path) as pdf:
            assert "converts light into chemical energy" in pdf[0].get_text()
    finally:
        if response.background:
            asyncio.run(response.background())
