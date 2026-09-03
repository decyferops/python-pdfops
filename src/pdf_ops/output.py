"""Output-path policy and atomic writes.

The reliability cornerstone: the final output path either holds a complete
file or nothing. Work is written to a temp file in the *destination
directory* (same filesystem - ``os.replace`` is only atomic within one) and
renamed over in one step, so a crashed or failed run never leaves a partial
PDF where a downstream workflow step could read it.
"""

from __future__ import annotations

import errno
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import NoReturn

from pdf_ops.config import OnExists
from pdf_ops.errors import OutputError


def check_output_dir(directory: Path) -> None:
    """Fail fast when the extraction target directory is absent."""
    if not directory.is_dir():
        raise OutputError(
            f"output directory {directory} does not exist "
            "(output locations are mounted; a missing directory is a workflow bug)",
            error_code="OUTPUT_DIR_MISSING",
            context={"output_dir": str(directory)},
        )


def check_output_path(path: Path, on_exists: OnExists) -> str:
    """Fail fast on unusable output locations, before any work is done.

    Returns the resolved action: ``"proceed"`` (no conflict), ``"skip"``
    (existing output accepted as a completed prior run), or ``"overwrite"``
    (existing output will be atomically replaced).
    """
    parent = path.parent
    if not parent.is_dir():
        raise OutputError(
            f"output directory {parent} does not exist "
            "(output locations are mounted; a missing directory is a workflow bug)",
            error_code="OUTPUT_DIR_MISSING",
            context={"output": str(path)},
        )
    if path.is_dir() and not path.is_symlink():
        # Never valid under any policy: it cannot be skipped as completed
        # work and os.replace cannot atomically replace a directory.
        raise OutputError(
            f"output path {path} is a directory",
            error_code="OUTPUT_IS_DIRECTORY",
            context={"output": str(path)},
        )
    if not (path.is_symlink() or path.exists()):
        return "proceed"
    if on_exists is OnExists.SKIP:
        # Completed prior work must be a real, resolvable file. A dangling
        # symlink is debris: fall through to replacement so the retry
        # actually produces the output (os.replace swaps the link itself,
        # atomically, without writing through it).
        return "skip" if path.is_file() else "overwrite"
    if on_exists is OnExists.OVERWRITE:
        return "overwrite"
    raise OutputError(
        f"output {path} already exists (refusing to overwrite; "
        "set PDFOPS_ON_EXISTS to overwrite or skip for retry semantics)",
        error_code="OUTPUT_EXISTS",
        context={"output": str(path)},
    )


def clean_stale_temps(
    target: Path, protected: frozenset[str] | set[str] = frozenset()
) -> list[str]:
    """Remove temp debris a crashed prior run left for this exact target.

    Scoped to this run's own target name via LITERAL string matching -
    never glob patterns, which would misinterpret metacharacters in
    (possibly attacker-supplied) names in both directions: deleting another
    target's temps and missing this target's own. Names in ``protected``
    (this run's planned final outputs) are never touched. One writer per
    output path at a time is the documented assumption; other steps' files
    are never removed. Returns the removed names for logging.
    """
    prefix = f".{target.name}."
    removed: list[str] = []
    for entry in sorted(target.parent.iterdir()):
        name = entry.name
        if not (name.startswith(prefix) and name.endswith(".tmp")):
            continue
        if name in protected:
            continue
        try:
            entry.unlink()
        except OSError:  # a concurrent unlink is fine; anything else surfaces later
            continue
        removed.append(name)
    return removed


@contextmanager
def atomic_output(path: Path) -> Generator[Path]:
    """Yield a temp path in the destination directory; publish it on success.

    On success the temp file is fsynced and renamed onto ``path`` (and the
    directory entry fsynced). On any failure the temp file is removed and the
    final path is left untouched.
    """
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as err:
        _raise_translated(err, path)
    # mkstemp creates 0600 (private by design) and os.replace carries that
    # mode onto the final path - where a downstream workflow step, typically
    # running as a different UID on a shared volume, could not read it.
    # Outputs are ordinary files: give them what plain open() would,
    # honoring the process umask.
    umask = os.umask(0)
    os.umask(umask)
    os.fchmod(fd, 0o666 & ~umask)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        yield tmp_path
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        tmp_path.replace(path)
        _fsync_dir(path.parent)
    except OSError as err:
        _cleanup(tmp_path)
        _raise_translated(err, path)
    except BaseException:
        _cleanup(tmp_path)
        raise


def _raise_translated(err: OSError, path: Path) -> NoReturn:
    translated = _translate_os_error(err, path)
    if translated is err:
        raise err
    raise translated from err


def _translate_os_error(err: OSError, path: Path) -> Exception:
    """Map I/O failures around the output location onto the taxonomy.

    Anything not recognizably an output-environment problem is returned
    unchanged so the unexpected-error boundary reports it honestly.
    """
    if err.errno == errno.ENOSPC:
        return OutputError(
            f"no space left on device while writing {path}",
            error_code="DISK_FULL",
            context={"output": str(path)},
        )
    if err.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return OutputError(
            f"output location {path} is not writable",
            error_code="OUTPUT_NOT_WRITABLE",
            context={"output": str(path)},
        )
    return err


def _cleanup(tmp_path: Path) -> None:
    with suppress(OSError):  # best effort - never mask the original failure
        tmp_path.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
