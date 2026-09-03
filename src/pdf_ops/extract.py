"""The extract operation: attachment names are untrusted input.

Names come straight out of the PDF and are written to a mounted filesystem,
so every name passes through ``sanitize_attachment_name`` - a pure function
designed for exhaustive table testing - and the resolved target of every
write is verified to stay inside the output directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pdf_ops.config import ExtractConfig, OnExists
from pdf_ops.engine import Attachment, get_engine
from pdf_ops.errors import InputError, OutputError
from pdf_ops.inputs import validate_inputs
from pdf_ops.output import atomic_output, check_output_dir, clean_stale_temps
from pdf_ops.secrets import Secrets

# Filesystem NAME_MAX is 255 bytes on the relevant filesystems; leave room
# for collision suffixes and the atomic-write temp prefix.
_MAX_NAME_BYTES = 200

FALLBACK_PREFIX = "attachment_"


def run_extract(config: ExtractConfig, secrets: Secrets, logger: logging.Logger) -> dict[str, Any]:
    validate_inputs([config.input])
    check_output_dir(config.output_dir)

    engine = get_engine()
    opened = engine.open_input(config.input, secrets.password)
    logger.info(
        "input_opened",
        extra={
            "input": str(config.input),
            "pages": opened.pages,
            "encrypted": opened.encrypted,
            "algorithm": opened.algorithm,
            "password_type": opened.password_type,
        },
    )
    for message in opened.warnings:
        logger.warning("pdf_library_message", extra={"detail": message, "source": "qpdf"})
    if secrets.password is not None and not opened.encrypted:
        logger.warning(
            "password_unused",
            extra={"detail": "a password was supplied but the input is not encrypted"},
        )

    attachments = engine.list_attachments(opened)
    for message in engine.collect_warnings(opened):
        # attachment streams are read lazily, so repairs can surface here
        logger.warning("pdf_library_message", extra={"detail": message, "source": "qpdf"})
    if not attachments:
        if config.fail_on_no_attachments:
            raise InputError(
                f"{config.input} contains no embedded attachments "
                "(failing because PDFOPS_FAIL_ON_NO_ATTACHMENTS=true)",
                error_code="NO_ATTACHMENTS",
                context={"input": str(config.input)},
            )
        return {"attachments_extracted": 0, "bytes_written": 0}

    planned = _plan_targets(attachments)
    planned_names = frozenset(p.name for p in planned)

    # A directory at any target name is never valid under any policy: it can
    # be neither skipped as completed work nor atomically replaced.
    directories = sorted(
        p.name
        for p in planned
        if (target := config.output_dir / p.name).is_dir() and not target.is_symlink()
    )
    if directories:
        raise OutputError(
            f"target name(s) are directories in {config.output_dir}: {', '.join(directories)}",
            error_code="OUTPUT_IS_DIRECTORY",
            context={"output_dir": str(config.output_dir), "directories": directories},
        )

    # Conflicts are resolved by policy BEFORE anything is written. lexists-
    # style detection so a pre-existing symlink (even dangling) counts.
    conflicts = sorted(
        str(p.name)
        for p in planned
        if (target := config.output_dir / p.name).is_symlink() or target.exists()
    )
    skipped_names: set[str] = set()
    replaced_names: list[str] = []
    if conflicts:
        match config.on_exists:
            case OnExists.FAIL:
                # All-or-nothing: one conflict refuses the whole run, so a
                # retry can't silently mix old and new files.
                raise OutputError(
                    f"{len(conflicts)} file(s) already exist in {config.output_dir}: "
                    f"{', '.join(conflicts)} (refusing to overwrite; "
                    "set PDFOPS_ON_EXISTS to overwrite or skip for retry semantics)",
                    error_code="OUTPUT_EXISTS",
                    context={"output_dir": str(config.output_dir), "conflicts": conflicts},
                )
            case OnExists.SKIP:
                # Per-file completion: existing REAL files are completed
                # prior work (each was written atomically, so it is whole);
                # only the missing ones are written - a crashed run's partial
                # set gets finished by the retry. A dangling symlink is not
                # completed work: it falls through to replacement.
                skipped_names = {name for name in conflicts if (config.output_dir / name).exists()}
                logger.info(
                    "attachments_skipped",
                    extra={"skipped": sorted(skipped_names), "count": len(skipped_names)},
                )
            case OnExists.OVERWRITE:
                replaced_names = conflicts

    # Stale-temp debris is cleaned up front, before the first write, so a
    # finalized output of THIS run can never be mistaken for debris; the
    # planned names themselves are additionally protected by construction.
    for item in planned:
        if item.name in skipped_names:
            continue
        for stale_name in clean_stale_temps(config.output_dir / item.name, planned_names):
            logger.warning("stale_temp_removed", extra={"temp_file": stale_name})

    resolved_root = config.output_dir.resolve()
    bytes_written = 0
    written = 0
    for item in planned:
        if item.name in skipped_names:
            continue
        target = config.output_dir / item.name
        # Non-dereferencing containment check: the write must land in the
        # output directory itself. (os.replace swaps a pre-existing symlink
        # rather than writing through it, so the entry's own resolution is
        # irrelevant - only a separator-carrying name could escape, and this
        # guards against exactly that sanitizer regression.)
        if target.parent.resolve() != resolved_root:
            raise RuntimeError(f"sanitization invariant violated for {item.original!r}")
        with atomic_output(target) as tmp_path:
            tmp_path.write_bytes(item.data)
        bytes_written += len(item.data)
        written += 1
        logger.info(
            "attachment_extracted",
            extra={
                "attachment": item.name,
                # capped: the original is attacker-controlled and can be
                # arbitrarily long - it must not balloon the log stream
                "original_name": (item.original[:200] if item.original != item.name else None),
                "bytes": len(item.data),
            },
        )

    if replaced_names:
        # Emitted after the writes, mirroring merge: by now the replacement
        # has actually happened.
        logger.info(
            "output_overwritten",
            extra={"replaced": replaced_names, "count": len(replaced_names)},
        )

    result: dict[str, Any] = {"attachments_extracted": written, "bytes_written": bytes_written}
    if skipped_names:
        result["attachments_skipped"] = len(skipped_names)
    return result


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    name: str
    original: str
    data: bytes


def _plan_targets(attachments: list[Attachment]) -> list[_PlannedFile]:
    """Sanitized, collision-suffixed target names in extraction order.

    Collisions are detected on casefolded names: the output directory is a
    mounted volume that may be case-insensitive (macOS, SMB), where two names
    differing only in case are one file - suffixing keeps every payload on
    every filesystem, and the plan stays identical everywhere (determinism).
    """
    used: set[str] = set()
    next_suffix: dict[str, int] = {}
    planned: list[_PlannedFile] = []
    for index, attachment in enumerate(attachments):
        name = _dedupe(sanitize_attachment_name(attachment.name, index), used, next_suffix)
        used.add(name.casefold())
        planned.append(_PlannedFile(name=name, original=attachment.name, data=attachment.data))
    return planned


def sanitize_attachment_name(raw: str, index: int) -> str:
    """Reduce an untrusted attachment name to a safe basename.

    Normalizes both separator conventions (a name written on Windows may
    carry backslashes), takes the last path component, strips control
    characters and surrounding whitespace, and falls back to a deterministic
    ``attachment_<index>`` when nothing safe remains. Pure function - no
    filesystem access - so the whole behavior is table-testable.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # Drop C0 controls (incl. NUL), DEL, and the C1 range - every Unicode
    # "Cc" character. Printable Unicode passes through untouched.
    name = "".join(ch for ch in name if ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F))
    name = name.strip()
    if name in ("", ".", ".."):
        return f"{FALLBACK_PREFIX}{index}"
    while len(name.encode()) > _MAX_NAME_BYTES:
        name = name[:-1]
    return name


def _dedupe(name: str, used: set[str], next_suffix: dict[str, int]) -> str:
    """Deterministic collision suffixes: report.txt, report-1.txt, ...

    ``used`` holds casefolded taken names; ``next_suffix`` remembers the next
    counter per colliding base so N duplicates resolve in O(N), not O(N^2).
    """
    key = name.casefold()
    if key not in used:
        return name
    if "." in name.lstrip("."):
        stem, dot, suffix = name.rpartition(".")
        candidate_format = f"{stem}-{{}}{dot}{suffix}"
    else:
        candidate_format = f"{name}-{{}}"
    counter = next_suffix.get(key, 1)
    while (candidate := candidate_format.format(counter)).casefold() in used:
        counter += 1
    next_suffix[key] = counter + 1
    return candidate
