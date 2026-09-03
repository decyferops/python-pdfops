"""Shared test helpers.

A plain module rather than conftest.py: conftest is a pytest plugin, and
importing names from it couples tests to how pytest loaded it.
"""

import logging
from collections.abc import Callable
from typing import Any

RunApp = Callable[[dict[str, str]], tuple[int, list[dict[str, Any]]]]


def make_record(
    msg: str = "some_event",
    *,
    name: str = "pdf_ops",
    level: int = logging.INFO,
    exc_info: Any = None,
    **extra: Any,
) -> logging.LogRecord:
    """A LogRecord as the formatter sees it, with ``extra`` fields attached."""
    record = logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=msg, args=None, exc_info=exc_info
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def build_raw_pdf(objects: list[str | bytes]) -> bytes:
    """Minimal hand-assembled PDF with a correct xref - for structural cases
    the writer API refuses to produce (dangling references, missing /Pages,
    raw name-tree bytes)."""
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        body_bytes = body if isinstance(body, bytes) else body.encode()
        out += f"{number} 0 obj\n".encode() + body_bytes + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)
