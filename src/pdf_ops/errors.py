"""Error taxonomy and exit codes.

The process exit code is the application's external API toward the workflow
engine: each error class maps to exactly one code, defined up front because
renumbering later would break workflow retry policies built on top of it.
Fine-grained detail travels in the machine-readable ``error_code`` string
carried by every raised error and emitted in the terminal log event.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """Exit codes a workflow engine can branch on (e.g. Argo retryStrategy)."""

    SUCCESS = 0
    UNEXPECTED = 1
    CONFIG = 2
    INPUT = 3
    INVALID_PDF = 4
    PASSWORD = 5
    OUTPUT = 6


class ErrorCode(StrEnum):
    """The machine-readable vocabulary carried by every ``operation_failed`` event.

    Grouped by the exit class each code travels with. The complete table with
    meanings lives in docs/OPERATIONS.md; a test keeps the two in sync, so a
    new code cannot ship undocumented.
    """

    # exit 1 - unexpected
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
    # exit 2 - configuration
    UNKNOWN_VAR = "UNKNOWN_VAR"
    INAPPLICABLE_VAR = "INAPPLICABLE_VAR"
    MISSING_VAR = "MISSING_VAR"
    INVALID_OPERATION = "INVALID_OPERATION"
    INVALID_LOG_LEVEL = "INVALID_LOG_LEVEL"
    INVALID_INPUTS = "INVALID_INPUTS"
    DUPLICATE_INPUTS = "DUPLICATE_INPUTS"
    INVALID_FLAG = "INVALID_FLAG"
    INVALID_ON_EXISTS = "INVALID_ON_EXISTS"
    INVALID_OUTPUT_ENCRYPTION = "INVALID_OUTPUT_ENCRYPTION"
    CONFLICTING_PASSWORD_SOURCES = "CONFLICTING_PASSWORD_SOURCES"
    OUTPUT_PASSWORD_WITHOUT_ENCRYPTION = "OUTPUT_PASSWORD_WITHOUT_ENCRYPTION"
    MISSING_OUTPUT_PASSWORD = "MISSING_OUTPUT_PASSWORD"
    PASSWORD_FILE_UNREADABLE = "PASSWORD_FILE_UNREADABLE"
    EMPTY_PASSWORD = "EMPTY_PASSWORD"
    PASSWORD_UNSUPPORTED_CHARACTERS = "PASSWORD_UNSUPPORTED_CHARACTERS"
    # exit 3 - input
    INPUT_MISSING = "INPUT_MISSING"
    INPUT_IS_DIRECTORY = "INPUT_IS_DIRECTORY"
    INPUT_UNREADABLE = "INPUT_UNREADABLE"
    NO_ATTACHMENTS = "NO_ATTACHMENTS"
    # exit 4 - invalid PDF
    NOT_A_PDF = "NOT_A_PDF"
    CORRUPT_PDF = "CORRUPT_PDF"
    UNSUPPORTED_PDF_FEATURE = "UNSUPPORTED_PDF_FEATURE"
    # exit 5 - password
    PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
    WRONG_PASSWORD = "WRONG_PASSWORD"
    UNSUPPORTED_ENCRYPTION = "UNSUPPORTED_ENCRYPTION"
    # exit 6 - output
    OUTPUT_DIR_MISSING = "OUTPUT_DIR_MISSING"
    OUTPUT_IS_DIRECTORY = "OUTPUT_IS_DIRECTORY"
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_NOT_WRITABLE = "OUTPUT_NOT_WRITABLE"
    DISK_FULL = "DISK_FULL"


class PdfOpsError(Exception):
    """Base class for every predictable failure.

    ``error_code`` is a stable machine-readable token from ``ErrorCode``;
    ``context`` holds structured detail for the failure log event. Neither may
    ever contain secret material - messages carry paths and names, not values.
    """

    exit_code: ExitCode = ExitCode.UNEXPECTED

    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.context: dict[str, Any] = context or {}


class ConfigError(PdfOpsError):
    """Invalid or missing environment configuration."""

    exit_code = ExitCode.CONFIG


class InputError(PdfOpsError):
    """Input file missing, unreadable, or not a regular file."""

    exit_code = ExitCode.INPUT


class InvalidPdfError(PdfOpsError):
    """Input exists but is not a valid PDF (corrupt, truncated, wrong type)."""

    exit_code = ExitCode.INVALID_PDF


class PasswordError(PdfOpsError):
    """Password required, wrong, or the encryption scheme is unsupported."""

    exit_code = ExitCode.PASSWORD


class OutputError(PdfOpsError):
    """Output conflict or output location not usable (exists, missing dir, full disk)."""

    exit_code = ExitCode.OUTPUT
