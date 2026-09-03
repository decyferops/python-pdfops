"""The PDF engine seam.

Everything library-specific lives behind this Protocol, and library exceptions
are translated into the application taxonomy inside the implementing module -
one seam, so replacing the PDF library is a single-module change and the rest
of the application never imports it directly.

Opening is a separate step from merging/extracting: the operation layer needs
each input's encryption facts (for events and the output-encryption policy)
before any output work starts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pdf_ops.secrets import Secret

# How an encrypted input opened: with the user or owner password supplied,
# or through the spec-standard empty-password try.
type PasswordKind = Literal["user", "owner", "empty"]


@dataclass(frozen=True, slots=True)
class OpenedInput:
    """A parsed, decrypted-if-needed input plus its encryption facts.

    ``handle`` is the library's reader object, opaque outside the engine.
    ``algorithm`` (e.g. ``AES-256``) comes from the plaintext /Encrypt
    dictionary - known before any password attempt. ``password_type`` records
    how an encrypted file opened: ``user``/``owner`` for a supplied password,
    ``empty`` when the spec-standard empty-password try succeeded (the common
    permissions-locked case).
    """

    path: Path
    handle: object
    pages: int
    encrypted: bool
    algorithm: str | None
    password_type: PasswordKind | None
    # Recoverable-damage messages the library reported while parsing
    # ("repairing", xref rebuilt, ...). The operation layer surfaces them as
    # events; anything unrecoverable raises instead.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Attachment:
    """One embedded file as stored in the PDF.

    ``name`` is UNTRUSTED input straight from the document - it may contain
    path separators, control characters, or be empty, and names are not
    unique. Callers must sanitize before touching a filesystem.
    """

    name: str
    data: bytes


class PdfEngine(Protocol):
    def open_input(self, path: Path, password: Secret | None) -> OpenedInput:
        """Parse ``path``, decrypting with ``password`` (or the empty
        password) when encrypted.

        Raises InvalidPdfError for unparseable files and PasswordError when
        decryption fails (wrong password, or none supplied and the empty try
        failed).
        """
        ...

    def merge_to(
        self,
        inputs: Sequence[OpenedInput],
        destination: Path,
        output_password: Secret | None,
    ) -> list[str]:
        """Merge ``inputs`` (in order) into a PDF at ``destination``,
        AES-256-encrypted with ``output_password`` when given.

        ``destination`` is a temp path provided by the atomic-write layer.
        Returns library warnings raised during the write (sources may be
        read lazily, so repairs can surface here rather than at open time).
        """
        ...

    def list_attachments(self, opened: OpenedInput) -> list[Attachment]:
        """Embedded files of ``opened``, in the document's name-tree order
        (deterministic across runs). Duplicate names are preserved."""
        ...

    def collect_warnings(self, opened: OpenedInput) -> list[str]:
        """Library warnings accumulated on ``opened`` since the last harvest
        (lazy readers keep discovering repairs after open time)."""
        ...


def get_engine() -> PdfEngine:
    """The single swap point for the PDF library backing the operations."""
    from pdf_ops.engine_pikepdf import PikepdfEngine

    return PikepdfEngine()
