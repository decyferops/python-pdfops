"""Shared fixtures: the invariant-checking app runner and PDF fixture factories.

Fixture files are generated programmatically - no binaries in the repo - so
each test states exactly what property its file has, and fixtures never drift
from the library version.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from pdf_ops.main import run
from tests.helpers import RunApp, build_raw_pdf

TERMINAL_EVENTS = {"operation_complete", "operation_failed"}


@pytest.fixture
def run_app(capsys: pytest.CaptureFixture[str]) -> RunApp:
    """Run the app in-process and enforce the cross-cutting contract invariants:

    stderr stays empty, every stdout line is valid JSON, and exactly one
    terminal event is emitted - as the last line.
    """

    def _run(env: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
        code = run(env)
        captured = capsys.readouterr()
        assert captured.err == "", f"stderr must stay empty, got: {captured.err!r}"
        events = [json.loads(line) for line in captured.out.strip().splitlines()]
        terminal = [e for e in events if e["event"] in TERMINAL_EVENTS]
        assert len(terminal) == 1, f"expected exactly one terminal event, got {terminal}"
        assert events[-1] is terminal[0], "the terminal event must be the last line"
        return code, events

    return _run


@pytest.fixture
def make_pdf(tmp_path: Path) -> Callable[..., Path]:
    """A valid PDF with ``pages`` blank pages of ``page_width`` points.

    Distinct page widths let tests verify merge ordering by inspecting the
    mediabox of each page in the merged output.
    """

    def _make(name: str = "doc.pdf", pages: int = 1, page_width: float = 200.0) -> Path:
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=page_width, height=300.0)
        path = tmp_path / name
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    return _make


@pytest.fixture
def make_non_pdf(tmp_path: Path) -> Callable[..., Path]:
    """A file that is not a PDF at all (regardless of its extension)."""

    def _make(name: str = "fake.pdf", content: bytes = b"plain text, not a pdf\n") -> Path:
        path = tmp_path / name
        path.write_bytes(content)
        return path

    return _make


@pytest.fixture
def make_corrupt_pdf(make_pdf: Callable[..., Path], tmp_path: Path) -> Callable[..., Path]:
    """A file with a valid ``%PDF-`` header that no parser can recover.

    qpdf-backed engines repair light damage (truncation, a mangled xref) by
    reconstructing the cross-reference table, so unrecoverable corruption has
    to destroy the object structure itself: ``garbage-body`` has no objects
    or trailer at all; ``no-objects`` keeps the file's shape but breaks every
    object keyword, leaving reconstruction nothing to find.
    """

    def _make(name: str = "corrupt.pdf", mode: str = "garbage-body") -> Path:
        if mode == "garbage-body":
            data = b"%PDF-1.7\n" + b"\x89\x00garbage" * 40
        elif mode == "no-objects":
            source = make_pdf(name=f"pristine-{name}", pages=2)
            data = source.read_bytes().replace(b" obj", b" obX")
        else:  # pragma: no cover - guard against typos in tests
            raise ValueError(f"unknown corruption mode: {mode}")
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _make


@pytest.fixture
def make_damaged_pdf(make_pdf: Callable[..., Path], tmp_path: Path) -> Callable[..., Path]:
    """A parseable-after-repair file: real content with a mangled xref. The
    engine reconstructs the cross-reference table and reports warnings."""

    def _make(name: str = "damaged.pdf") -> Path:
        source = make_pdf(name=f"pristine-{name}", pages=2)
        path = tmp_path / name
        path.write_bytes(source.read_bytes().replace(b"xref", b"xrfx", 1))
        return path

    return _make


@pytest.fixture
def make_encrypted_pdf(tmp_path: Path) -> Callable[..., Path]:
    """An encrypted one-page PDF.

    Default: RC4 with a user password. ``algorithm="AES-256"`` for the modern
    scheme; ``user_password=""`` with an ``owner_password`` builds the common
    permissions-locked file that every viewer opens without a prompt.
    """

    def _make(
        name: str = "locked.pdf",
        password: str = "secret",
        owner_password: str | None = None,
        algorithm: str | None = None,
    ) -> Path:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        if algorithm is not None:
            writer.encrypt(
                user_password=password, owner_password=owner_password, algorithm=algorithm
            )
        else:
            writer.encrypt(user_password=password, owner_password=owner_password)
        path = tmp_path / name
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    return _make


@pytest.fixture
def make_dangling_ref_pdf(tmp_path: Path) -> Callable[..., Path]:
    """A parseable PDF whose page /Contents points at a missing object -
    the engine repairs it and reports a recoverable-corruption warning."""

    def _make(name: str = "dangling.pdf") -> Path:
        path = tmp_path / name
        path.write_bytes(
            build_raw_pdf(
                [
                    "<< /Type /Catalog /Pages 2 0 R >>",
                    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Contents 9 0 R >>",
                ]
            )
        )
        return path

    return _make


@pytest.fixture
def make_pathological_pdf(tmp_path: Path) -> Callable[..., Path]:
    """Valid header and xref, but a catalog with no /Pages - a structural
    hole that must classify as corrupt, not as an internal error."""

    def _make(name: str = "pathological.pdf") -> Path:
        path = tmp_path / name
        path.write_bytes(build_raw_pdf(["<< /Type /Catalog >>"]))
        return path

    return _make


@pytest.fixture
def make_pdf_with_attachments(tmp_path: Path) -> Callable[..., Path]:
    """A one-page PDF carrying the given embedded files.

    pypdf writes names verbatim into the name tree, so hostile names
    (traversal, separators, empty) and duplicates survive the roundtrip -
    exactly what the sanitizer tests need. Extraction order is the PDF
    name-tree order (sorted by name), not insertion order.
    """

    def _make(attachments: list[tuple[str, bytes]], name: str = "carrier.pdf") -> Path:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        for attachment_name, data in attachments:
            writer.add_attachment(attachment_name, data)
        path = tmp_path / name
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    return _make


@pytest.fixture
def make_raw_attachment_pdf(tmp_path: Path) -> Callable[..., Path]:
    """An attachment built directly in the /Names/EmbeddedFiles tree, with the
    name given as a raw PDF string literal - reaches name shapes (UTF-16,
    non-UTF-8 bytes) and stream filters the writer API can't produce."""

    def _make(
        name_literal: bytes,
        stream: bytes = b"payload",
        filter_entry: bytes = b"",
        name: str = "raw-carrier.pdf",
    ) -> Path:
        length = str(len(stream)).encode()
        filter_part = (b" /Filter " + filter_entry) if filter_entry else b""
        path = tmp_path / name
        path.write_bytes(
            build_raw_pdf(
                [
                    b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles "
                    b"<< /Names [ " + name_literal + b" 4 0 R ] >> >> >>",
                    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
                    b"<< /Type /Filespec /F " + name_literal + b" /EF << /F 5 0 R >> >>",
                    b"<< /Length "
                    + length
                    + filter_part
                    + b" >>\nstream\n"
                    + stream
                    + b"\nendstream",
                ]
            )
        )
        return path

    return _make
