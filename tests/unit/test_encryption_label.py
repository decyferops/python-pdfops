"""The failed-open encryption label: a raw scan of an attacker-controlled
file that must stay correct on real layouts and harmless on hostile ones."""

from collections.abc import Callable
from pathlib import Path

import pytest

from pdf_ops.engine_pikepdf import (
    _describe_encryption_raw,
)

pytestmark = pytest.mark.unit


class TestDescribeEncryptionRaw:
    def test_aes256_labelled(self, make_encrypted_pdf: Callable[..., Path]) -> None:
        locked = make_encrypted_pdf(password="pw", algorithm="AES-256")
        assert _describe_encryption_raw(locked) == "AES-256"

    def test_rc4_128_exact_not_a_stream_length(
        self, make_encrypted_pdf: Callable[..., Path]
    ) -> None:
        # The label must come from the /Encrypt object's own body - the
        # first /Length token in the file belongs to some content stream.
        locked = make_encrypted_pdf(password="pw")
        assert _describe_encryption_raw(locked) == "RC4-128"

    def test_appended_digit_wall_neither_crashes_nor_mislabels(
        self, make_encrypted_pdf: Callable[..., Path], tmp_path: Path
    ) -> None:
        # int() on an unbounded attacker-controlled digit run raises under
        # the integer-conversion limit; the bounded token match must neither
        # crash nor let a decoy /V win over the real /Encrypt object.
        locked = make_encrypted_pdf(password="pw", algorithm="AES-256")
        decoy = tmp_path / "decoy.pdf"
        decoy.write_bytes(locked.read_bytes() + b"\n% /V " + b"5" * 6000 + b"\n")
        assert _describe_encryption_raw(decoy) == "AES-256"

    def test_unencrypted_or_garbage_is_unknown(self, tmp_path: Path) -> None:
        blob = tmp_path / "blob.pdf"
        blob.write_bytes(b"%PDF-1.7\nno encryption anywhere\n%%EOF\n")
        assert _describe_encryption_raw(blob) == "unknown"
        assert _describe_encryption_raw(tmp_path / "missing.pdf") == "unknown"
