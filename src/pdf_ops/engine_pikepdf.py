"""pikepdf-backed engine implementation.

The only module that imports pikepdf. Translates qpdf's failure modes into
the application taxonomy so callers never see library-specific errors, and is
the only code that calls ``Secret.reveal()``.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pikepdf

from pdf_ops.engine import Attachment, OpenedInput
from pdf_ops.errors import InvalidPdfError, PasswordError
from pdf_ops.secrets import Secret

# pikepdf converts PDF integers/reals/booleans/null to native Python values,
# so type-blind access on attacker-controlled structures (a name tree whose
# node is an integer, a filespec whose /EF is a number, a dangling reference
# resolving to None) raises builtin exceptions, not PdfError. These catches
# wrap ONLY walks over document structure, so a builtin exception there means
# a file shape the engine cannot process, never a bug in our code. OSError
# deliberately excluded: I/O failures must keep their own classification.
_STRUCTURE_FAILURES = (
    AttributeError,
    KeyError,
    IndexError,
    TypeError,
    ValueError,
    RecursionError,
)


class PikepdfEngine:
    def open_input(self, path: Path, password: Secret | None) -> OpenedInput:
        supplied = password.reveal() if password is not None else ""
        used = supplied
        try:
            pdf = _open_quietly(path, supplied)
        except pikepdf.PasswordError:
            # No handle exists on this path, so the algorithm label comes
            # from a raw scan of the plaintext /Encrypt dictionary.
            algorithm = _describe_encryption_raw(path)
            if password is None:
                raise PasswordError(
                    f"{path} is encrypted ({algorithm}) and requires a password",
                    error_code="PASSWORD_REQUIRED",
                    context={"input": str(path), "algorithm": algorithm},
                ) from None
            # The supplied password failed - but this input may not need it
            # at all (a permissions-locked file among user-locked ones in a
            # merge). The spec-standard empty try still applies; qpdf only
            # tries the string it was given.
            try:
                pdf = _open_quietly(path, "")
                used = ""
            except pikepdf.PasswordError:
                raise PasswordError(
                    f"the supplied password does not open {path} ({algorithm})",
                    error_code="WRONG_PASSWORD",
                    context={"input": str(path), "algorithm": algorithm},
                ) from None
            except pikepdf.PdfError as err:
                raise _corrupt(path, err) from err
        except pikepdf.PdfError as err:
            if _mentions_encryption(path) and "encrypt" in str(err).lower():
                # Certificate security handlers, exotic revisions: an
                # encryption problem, not a malformed file - the operator
                # remedy lives in the password class.
                raise PasswordError(
                    f"{path} uses an encryption scheme this build cannot process",
                    error_code="UNSUPPORTED_ENCRYPTION",
                    context={"input": str(path)},
                ) from err
            raise _corrupt(path, err) from err

        encrypted = bool(pdf.is_encrypted)
        algorithm = _describe_encryption(pdf) if encrypted else None
        password_type: str | None = None
        if encrypted:
            if not used:
                password_type = "empty"
            else:
                # qpdf records which of the two document passwords the
                # supplied string matched.
                password_type = "owner" if pdf.owner_password_matched else "user"

        try:
            pages = len(pdf.pages)
        except pikepdf.PdfError as err:
            raise _translated_data_error(path, err) from err
        except _STRUCTURE_FAILURES as err:
            raise _corrupt(path, err) from err

        return OpenedInput(
            path=path,
            handle=pdf,
            pages=pages,
            encrypted=encrypted,
            algorithm=algorithm,
            password_type=password_type,
            # qpdf reports recoverable damage ("repairing", xref rebuilds)
            # through its warning channel, not Python logging; carried here
            # so the operation layer can surface them as JSON events.
            warnings=tuple(str(message) for message in pdf.get_warnings()),
        )

    def merge_to(
        self,
        inputs: Sequence[OpenedInput],
        destination: Path,
        output_password: Secret | None,
    ) -> list[str]:
        with pikepdf.Pdf.new() as merged:
            for opened in inputs:
                source = cast(pikepdf.Pdf, opened.handle)
                try:
                    merged.pages.extend(source.pages)
                except pikepdf.PdfError as err:
                    raise _translated_data_error(opened.path, err) from err
                except _STRUCTURE_FAILURES as err:
                    raise _corrupt(opened.path, err) from err

            encryption = None
            if output_password is not None:
                raw = output_password.reveal()
                # R=6 pinned explicitly: AES-256, never a legacy scheme.
                encryption = pikepdf.Encryption(user=raw, owner=raw, R=6)
            try:
                # Saved through the already-open temp file, NOT by path: given
                # a path to an existing file, pikepdf routes through its own
                # atomic-overwrite (a second hidden temp in the destination
                # directory that a kill would orphan outside the stale-temp
                # naming scheme). Writing into the atomic-write layer's temp
                # keeps one temp file, one rename, and the mode it set.
                with destination.open("wb") as handle:
                    if encryption is not None:
                        merged.save(handle, encryption=encryption)
                    else:
                        merged.save(handle)
            except pikepdf.PdfError as err:
                # qpdf copies source streams lazily at save, so a source that
                # turns out unreadable surfaces here without attribution.
                raise InvalidPdfError(
                    f"a merge input could not be fully read while writing: {err}",
                    error_code="CORRUPT_PDF",
                    context={"inputs": [str(one.path) for one in inputs]},
                ) from err
            # Repairs discovered during the lazy copy accumulate on the
            # sources and the writer after open-time harvesting.
            late: list[str] = [str(m) for m in merged.get_warnings()]
        for opened in inputs:
            late.extend(self.collect_warnings(opened))
        return late

    def list_attachments(self, opened: OpenedInput) -> list[Attachment]:
        pdf = cast(pikepdf.Pdf, opened.handle)
        try:
            entries = _embedded_file_entries(pdf)
        except pikepdf.PdfError as err:
            raise _translated_data_error(opened.path, err) from err
        except _STRUCTURE_FAILURES as err:
            raise _corrupt(opened.path, err) from err

        attachments: list[Attachment] = []
        for raw_name, spec in entries:
            try:
                embedded: Any = spec.get("/EF") if isinstance(spec, pikepdf.Dictionary) else None
                if embedded is None or not isinstance(embedded, pikepdf.Dictionary):
                    # A bare file reference (no embedded stream) or a
                    # malformed /EF: nothing extractable in this entry.
                    continue
                stream: Any = embedded.get("/F")
                if stream is None:
                    stream = embedded.get("/UF")
                if stream is None:
                    continue
                data = bytes(stream.read_bytes())
            except pikepdf.PdfError as err:
                raise _translated_data_error(opened.path, err) from err
            except _STRUCTURE_FAILURES as err:
                raise _corrupt(opened.path, err) from err
            # A name-tree key must be a PDF string; qpdf hands anything else
            # over as a native Python value (an integer key would make a
            # bytes()/str() conversion attacker-sized). A non-string key gets
            # the deterministic fallback name downstream - the payload is
            # still extracted. str() of a pikepdf.String decodes
            # PDFDocEncoding and UTF-16 per spec.
            name = str(raw_name) if isinstance(raw_name, pikepdf.String) else ""
            attachments.append(Attachment(name=name, data=data))
        return attachments

    def collect_warnings(self, opened: OpenedInput) -> list[str]:
        """Warnings qpdf accumulated on the handle since the last harvest."""
        return [str(m) for m in cast(pikepdf.Pdf, opened.handle).get_warnings()]


def _open_quietly(path: Path, password: str) -> pikepdf.Pdf:
    """Open, muting pikepdf's password-was-not-needed UserWarning: the
    operation layer already reports that case as a ``password_unused`` event
    with more context, and a duplicate through the warnings channel would
    only add noise."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="A password was provided")
        return pikepdf.open(path, password=password)


def _embedded_file_entries(pdf: pikepdf.Pdf) -> list[tuple[Any, Any]]:
    """(name, filespec) pairs from /Names/EmbeddedFiles, in tree order.

    Walked directly rather than through ``pdf.attachments``: that Mapping
    collapses duplicate attachment names, which real PDFs contain and the
    extraction contract preserves.
    """
    root: Any = pdf.Root
    names_dict: Any = root.get("/Names")
    if not isinstance(names_dict, pikepdf.Dictionary):
        return []
    tree: Any = names_dict.get("/EmbeddedFiles")
    if tree is None:
        return []
    entries: list[tuple[Any, Any]] = []
    _walk_name_tree(tree, entries, set())
    return entries


def _walk_name_tree(node: Any, out: list[tuple[Any, Any]], seen: set[tuple[int, int]]) -> None:
    if not isinstance(node, pikepdf.Dictionary):
        # A node that is not a dictionary (integer, null from a dangling
        # reference, ...) cannot carry entries; hostile trees do this.
        return
    objgen = cast("tuple[int, int]", tuple(node.objgen))
    if objgen != (0, 0):  # (0, 0) means a direct object - not a stable identity
        if objgen in seen:  # hostile trees can contain reference cycles
            return
        seen.add(objgen)
    kids: Any = node.get("/Kids")
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            _walk_name_tree(kid, out, seen)
    names: Any = node.get("/Names")
    if isinstance(names, pikepdf.Array):
        out.extend((names[index], names[index + 1]) for index in range(0, len(names) - 1, 2))


def _describe_encryption(pdf: pikepdf.Pdf) -> str:
    """Human label for the encryption of a successfully opened file."""
    try:
        info = pdf.encryption
        version = int(info.V)
        bits = int(info.bits)
        if version == 5:
            return "AES-256"
        if version == 4:
            return "AES-128" if "aes" in str(info.stream_method).lower() else f"RC4-{bits}"
        if version in (1, 2):
            return f"RC4-{bits}"
        return f"V{version}"
    except Exception:
        return "unknown"


# Bounded digit runs with a lookahead cutoff: these tokens are read from raw,
# attacker-controlled bytes, and an unbounded \d+ capture would both match
# decoy digit walls and feed int() something conversion-limited.
_ENCRYPT_REF = re.compile(rb"/Encrypt\s+(\d{1,9})(?!\d)\s+(\d{1,5})(?!\d)\s+R")
_VERSION_TOKEN = re.compile(rb"/V\s+(\d{1,3})(?!\d)")
_LENGTH_TOKEN = re.compile(rb"/Length\s+(\d{1,5})(?!\d)")

# Reading cap for the failed-open scan: files larger than this only get their
# tail examined, degrading the label to "unknown" when the /Encrypt object
# sits earlier - acceptable for an error-context label.
_RAW_SCAN_LIMIT = 32 * 1024 * 1024


def _describe_encryption_raw(path: Path) -> str:
    """Best-effort label when the file could not be opened at all.

    The /Encrypt dictionary is plaintext, but without a handle the only way
    to it is a raw scan: the trailer names the object (``/Encrypt N G R``)
    and the tokens are read from that object's own body, so a stray ``/V``
    elsewhere (a form-field value, a decoy) is not mistaken for the
    encryption version. Anything unresolvable degrades to "unknown" - this
    only ever feeds error-context observability.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _RAW_SCAN_LIMIT:
                handle.seek(size - _RAW_SCAN_LIMIT)
            data = handle.read(_RAW_SCAN_LIMIT)
    except OSError:
        return "unknown"

    reference = None
    for match in _ENCRYPT_REF.finditer(data):
        reference = match  # the trailer (last occurrence) is authoritative
    if reference is None:
        return "unknown"
    header = b"%d %d obj" % (int(reference.group(1)), int(reference.group(2)))
    at = data.rfind(header)
    if at < 0:
        return "unknown"
    body = data[at : at + 2048]

    version_match = _VERSION_TOKEN.search(body)
    if version_match is None:
        return "unknown"
    version = int(version_match.group(1))
    length_match = _LENGTH_TOKEN.search(body)
    length = int(length_match.group(1)) if length_match else 40
    if version == 5:
        return "AES-256"
    if version == 4:
        return "AES-128" if b"/AESV2" in body else f"RC4-{length}"
    if version == 2:
        return f"RC4-{length}"
    if version == 1:
        return "RC4-40"
    return f"V{version}"


def _mentions_encryption(path: Path) -> bool:
    """Best-effort check whether a file qpdf refused to open carries an
    /Encrypt dictionary (it lives near the trailer)."""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 8192))
            return b"/Encrypt" in handle.read()
    except OSError:
        return False


def _translated_data_error(path: Path, err: Exception) -> InvalidPdfError:
    if "unfilterable" in str(err):
        # A stream filter qpdf cannot decode: a permanent, data-dependent
        # condition distinct from structural corruption.
        return InvalidPdfError(
            f"{path} uses a PDF feature this build cannot process: {err}",
            error_code="UNSUPPORTED_PDF_FEATURE",
            context={"input": str(path)},
        )
    return _corrupt(path, err)


def _corrupt(path: Path, err: Exception) -> InvalidPdfError:
    return InvalidPdfError(
        f"cannot parse {path} as a PDF: {err}",
        error_code="CORRUPT_PDF",
        context={"input": str(path)},
    )
