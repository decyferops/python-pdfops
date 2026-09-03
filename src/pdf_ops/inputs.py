"""Up-front input validation shared by every operation.

Inputs are checked before anything is written, and every problem is
reported in one failure: an operator fixing a broken workflow should learn
about every bad input from a single run, not one per retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pdf_ops.errors import ErrorCode, InputError, InvalidPdfError, PdfOpsError

PDF_MAGIC = b"%PDF-"

# Problem kinds found during input validation, in exit-code class order.
_INPUT_PROBLEMS = frozenset(
    {ErrorCode.INPUT_MISSING, ErrorCode.INPUT_IS_DIRECTORY, ErrorCode.INPUT_UNREADABLE}
)


def validate_inputs(inputs: Sequence[Path]) -> None:
    """Check every input up front and report all problems in one failure.

    An operator fixing a broken workflow should learn about every bad input
    from a single run, not one per retry. The raised error's class (and thus
    the exit code) follows the first problem in input order; the full list
    travels in ``context``.
    """
    problems: list[dict[str, str]] = []
    first_code: ErrorCode | None = None
    for path in inputs:
        code = _check_one(path)
        if code is None:
            continue
        if first_code is None:
            first_code = code
        problems.append({"input": str(path), "error_code": code})
    if first_code is None:
        return

    error_class: type[PdfOpsError] = (
        InputError if first_code in _INPUT_PROBLEMS else InvalidPdfError
    )
    raise error_class(
        f"{len(problems)} of {len(inputs)} input(s) unusable; "
        f"first: {problems[0]['input']} ({first_code})",
        error_code=first_code,
        context={"problems": problems},
    )


def _check_one(path: Path) -> ErrorCode | None:
    if path.is_dir():
        return ErrorCode.INPUT_IS_DIRECTORY
    if not path.is_file():
        return ErrorCode.INPUT_MISSING
    try:
        with path.open("rb") as handle:
            head = handle.read(len(PDF_MAGIC))
    except OSError:
        return ErrorCode.INPUT_UNREADABLE
    if not head.startswith(PDF_MAGIC):
        return ErrorCode.NOT_A_PDF
    return None
