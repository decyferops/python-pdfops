"""Table-driven tests for the attachment-name sanitizer.

Attachment names are attacker-controlled strings written to a mounted
filesystem - this table is the security contract for that boundary.
"""

import pytest

from pdf_ops.engine import Attachment
from pdf_ops.extract import _plan_targets, sanitize_attachment_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # benign names pass through
        ("report.txt", "report.txt"),
        ("R\u00e9sum\u00e9 2026.pdf", "R\u00e9sum\u00e9 2026.pdf"),
        (".hidden", ".hidden"),
        ("no-extension", "no-extension"),
        # path traversal and absolute paths reduce to the basename
        ("../../evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("a/b/c.txt", "c.txt"),
        # Windows separators are separators too
        ("a\\b.txt", "b.txt"),
        ("..\\..\\win.ini", "win.ini"),
        ("C:\\Users\\x\\doc.pdf", "doc.pdf"),
        # control characters are stripped, surrounding whitespace trimmed
        ("con\x00trol.txt", "control.txt"),
        ("tab\there.txt", "tabhere.txt"),
        ("  spaced.txt  ", "spaced.txt"),
    ],
)
def test_sanitization_table(raw: str, expected: str) -> None:
    assert sanitize_attachment_name(raw, 0) == expected


@pytest.mark.parametrize("raw", ["", ".", "..", "   ", "///", "\\\\", "\x00\x01"])
def test_nothing_safe_left_falls_back_deterministically(raw: str) -> None:
    assert sanitize_attachment_name(raw, 0) == "attachment_0"
    assert sanitize_attachment_name(raw, 7) == "attachment_7"


def test_overlong_names_fit_the_filesystem() -> None:
    sanitized = sanitize_attachment_name("x" * 1000 + ".txt", 0)
    assert len(sanitized.encode()) <= 200


def test_multibyte_truncation_stays_valid_utf8() -> None:
    sanitized = sanitize_attachment_name("\u00e9" * 500, 0)
    assert len(sanitized.encode()) <= 200
    sanitized.encode()  # must not raise


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ev\x7fil.txt", "evil.txt"),  # DEL
        ("ne\x85l.txt", "nel.txt"),  # C1 NEL
        ("a\x9bb.txt", "ab.txt"),  # C1 CSI
    ],
)
def test_del_and_c1_controls_are_stripped(raw: str, expected: str) -> None:
    assert sanitize_attachment_name(raw, 0) == expected


class TestPlanTargets:
    """The dedupe policy, pinned shape by shape."""

    @staticmethod
    def plan(names: list[str]) -> list[str]:
        attachments = [Attachment(name=n, data=b"x") for n in names]
        return [p.name for p in _plan_targets(attachments)]

    def test_extension_suffixing(self) -> None:
        assert self.plan(["report.txt", "report.txt", "report.txt"]) == [
            "report.txt",
            "report-1.txt",
            "report-2.txt",
        ]

    def test_no_extension_suffixing(self) -> None:
        assert self.plan(["data", "data"]) == ["data", "data-1"]

    def test_dotfile_suffixing(self) -> None:
        assert self.plan([".hidden", ".hidden"]) == [".hidden", ".hidden-1"]

    def test_multi_dot_suffix_goes_before_last_extension(self) -> None:
        assert self.plan(["archive.tar.gz", "archive.tar.gz"]) == [
            "archive.tar.gz",
            "archive.tar-1.gz",
        ]

    def test_real_name_occupying_a_suffix_candidate(self) -> None:
        # A real attachment already named like a dedupe candidate must not be
        # overwritten by a later duplicate's suffix.
        assert self.plan(["report.txt", "report-1.txt", "report.txt"]) == [
            "report.txt",
            "report-1.txt",
            "report-2.txt",
        ]

    def test_case_only_difference_is_a_collision(self) -> None:
        # Output volumes can be case-insensitive (macOS, SMB): names differing
        # only in case must be suffixed, identically on every filesystem.
        assert self.plan(["Report.txt", "report.txt"]) == ["Report.txt", "report-1.txt"]

    def test_real_name_colliding_with_a_fallback(self) -> None:
        # An attachment literally named like the fallback of a later
        # unnameable one must not collide with it.
        assert self.plan(["attachment_1", ""]) == ["attachment_1", "attachment_1-1"]
