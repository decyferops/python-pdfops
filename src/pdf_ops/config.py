"""Environment-variable configuration parsing.

``parse_config`` is a pure function over a mapping so tests drive it with
plain dicts; the real ``os.environ`` is touched only in ``__main__``. All
validation happens here, before any file is opened - invalid configuration
must fail fast with exit code 2. Deliberately filesystem-free: existence and
readability of paths are operation-stage concerns, not configuration ones.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

from pdf_ops.errors import ConfigError, ErrorCode
from pdf_ops.secrets import EnvSecret, FileSecret, Secret, SecretRef

ENV_PREFIX = "PDFOPS_"

VAR_OPERATION = "PDFOPS_OPERATION"
VAR_LOG_LEVEL = "PDFOPS_LOG_LEVEL"
VAR_INPUTS = "PDFOPS_INPUTS"
VAR_OUTPUT = "PDFOPS_OUTPUT"
VAR_INPUT = "PDFOPS_INPUT"
VAR_OUTPUT_DIR = "PDFOPS_OUTPUT_DIR"
VAR_FAIL_ON_NO_ATTACHMENTS = "PDFOPS_FAIL_ON_NO_ATTACHMENTS"
VAR_PASSWORD = "PDFOPS_PASSWORD"
VAR_PASSWORD_FILE = "PDFOPS_PASSWORD_FILE"
VAR_ON_EXISTS = "PDFOPS_ON_EXISTS"
VAR_OUTPUT_ENCRYPTION = "PDFOPS_OUTPUT_ENCRYPTION"
VAR_OUTPUT_PASSWORD = "PDFOPS_OUTPUT_PASSWORD"
VAR_OUTPUT_PASSWORD_FILE = "PDFOPS_OUTPUT_PASSWORD_FILE"

# Every variable the application understands. Any other PDFOPS_-prefixed
# variable is rejected as a probable typo (a silently ignored misspelling like
# PDFOPS_INPUTS_ would otherwise surface as a confusing downstream error).
KNOWN_VARS = frozenset(
    {
        VAR_OPERATION,
        VAR_LOG_LEVEL,
        VAR_INPUTS,
        VAR_OUTPUT,
        VAR_INPUT,
        VAR_OUTPUT_DIR,
        VAR_FAIL_ON_NO_ATTACHMENTS,
        VAR_PASSWORD,
        VAR_PASSWORD_FILE,
        VAR_ON_EXISTS,
        VAR_OUTPUT_ENCRYPTION,
        VAR_OUTPUT_PASSWORD,
        VAR_OUTPUT_PASSWORD_FILE,
    }
)

MERGE_ONLY_VARS = frozenset(
    {VAR_INPUTS, VAR_OUTPUT, VAR_OUTPUT_ENCRYPTION, VAR_OUTPUT_PASSWORD, VAR_OUTPUT_PASSWORD_FILE}
)
EXTRACT_ONLY_VARS = frozenset({VAR_INPUT, VAR_OUTPUT_DIR, VAR_FAIL_ON_NO_ATTACHMENTS})

# The list separator for PDFOPS_INPUTS: os.pathsep (":" on POSIX), the same
# convention as PATH. Colons in mounted file paths are effectively unheard of;
# commas and spaces are not.
INPUTS_SEPARATOR = os.pathsep

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
DEFAULT_LOG_LEVEL = logging.INFO


class Operation(StrEnum):
    MERGE = "merge"
    EXTRACT = "extract"


class OnExists(StrEnum):
    """Policy for outputs that already exist - the retry-semantics knob.

    ``fail``: refuse (exit 6), the surprise-free default. ``overwrite``:
    replace atomically. ``skip``: treat existing output as a completed prior
    run - for merge a whole-run no-op (exit 0, ``skipped: true``); for
    extract, per-file completion (write only the missing attachments).
    """

    FAIL = "fail"
    OVERWRITE = "overwrite"
    SKIP = "skip"


class OutputEncryption(StrEnum):
    """Policy for encrypting the merged output.

    ``never``: plaintext output (a loud warning event flags the downgrade when
    inputs were encrypted). ``inherit``: encrypt iff at least one input was
    encrypted - confidentiality never decreases through this step. ``always``:
    unconditionally encrypt.
    """

    NEVER = "never"
    INHERIT = "inherit"
    ALWAYS = "always"


@dataclass(frozen=True, slots=True)
class MergeConfig:
    operation: ClassVar[Literal[Operation.MERGE]] = Operation.MERGE
    log_level: int
    inputs: tuple[Path, ...]
    output: Path
    password: SecretRef | None
    output_encryption: OutputEncryption
    output_password: SecretRef | None
    on_exists: OnExists


@dataclass(frozen=True, slots=True)
class ExtractConfig:
    operation: ClassVar[Literal[Operation.EXTRACT]] = Operation.EXTRACT
    log_level: int
    input: Path
    output_dir: Path
    fail_on_no_attachments: bool
    password: SecretRef | None
    on_exists: OnExists


type Config = MergeConfig | ExtractConfig


def parse_config(env: Mapping[str, str]) -> Config:
    """Validate ``env`` and freeze it into an operation config.

    Raises ConfigError (exit code 2) on any missing, invalid, unknown, or
    inapplicable variable.
    """
    _reject_unknown_vars(env)
    operation = _parse_operation(env)
    log_level = _parse_log_level(env)
    password = _parse_secret_pair(env, VAR_PASSWORD, VAR_PASSWORD_FILE)
    on_exists = _parse_on_exists(env)
    match operation:
        case Operation.MERGE:
            _reject_inapplicable_vars(env, operation, EXTRACT_ONLY_VARS)
            return MergeConfig(
                log_level=log_level,
                inputs=_parse_inputs(env),
                output=_parse_output(env),
                password=password,
                output_encryption=_parse_output_encryption(env),
                output_password=_parse_output_password(env, password),
                on_exists=on_exists,
            )
        case Operation.EXTRACT:
            _reject_inapplicable_vars(env, operation, MERGE_ONLY_VARS)
            return ExtractConfig(
                log_level=log_level,
                input=_parse_single_path(env, VAR_INPUT, "the PDF to extract from"),
                output_dir=_parse_single_path(
                    env, VAR_OUTPUT_DIR, "the directory receiving the attachments"
                ),
                fail_on_no_attachments=_parse_flag(env, VAR_FAIL_ON_NO_ATTACHMENTS),
                password=password,
                on_exists=on_exists,
            )


def _reject_unknown_vars(env: Mapping[str, str]) -> None:
    unknown = sorted(k for k in env if k.startswith(ENV_PREFIX) and k not in KNOWN_VARS)
    if unknown:
        raise ConfigError(
            f"unknown environment variable(s): {', '.join(unknown)}; "
            f"accepted: {', '.join(sorted(KNOWN_VARS))}",
            error_code=ErrorCode.UNKNOWN_VAR,
            context={"unknown_vars": unknown},
        )


def _reject_inapplicable_vars(
    env: Mapping[str, str], operation: Operation, inapplicable: frozenset[str] | set[str]
) -> None:
    # A merge-only variable on an extract run (or vice versa) is the same
    # class of workflow-templating bug as a typo: fail loudly, don't ignore.
    # Value-based, honoring the empty-equals-missing rule: an empty variable
    # configures nothing. (The unknown-var check stays name-based - there the
    # typo'd NAME is the signal, whatever the value.)
    present = sorted(k for k in inapplicable if env.get(k, "").strip())
    if present:
        raise ConfigError(
            f"variable(s) not applicable to operation '{operation.value}': {', '.join(present)}",
            error_code=ErrorCode.INAPPLICABLE_VAR,
            context={"operation": operation.value, "inapplicable_vars": present},
        )


def _parse_operation(env: Mapping[str, str]) -> Operation:
    raw = env.get(VAR_OPERATION, "").strip()
    if not raw:
        raise ConfigError(
            f"{VAR_OPERATION} is required (accepted values: merge, extract)",
            error_code=ErrorCode.MISSING_VAR,
            context={"var": VAR_OPERATION},
        )
    try:
        return Operation(raw)
    except ValueError:
        raise ConfigError(
            f"{VAR_OPERATION} has invalid value {raw!r} (accepted values: merge, extract)",
            error_code=ErrorCode.INVALID_OPERATION,
            context={"var": VAR_OPERATION, "value": raw},
        ) from None


def _parse_log_level(env: Mapping[str, str]) -> int:
    raw = env.get(VAR_LOG_LEVEL, "").strip()
    if not raw:
        return DEFAULT_LOG_LEVEL
    level = _LOG_LEVELS.get(raw.upper())
    if level is None:
        raise ConfigError(
            f"{VAR_LOG_LEVEL} has invalid value {raw!r} "
            f"(accepted values: {', '.join(_LOG_LEVELS).lower()}, case-insensitive)",
            error_code=ErrorCode.INVALID_LOG_LEVEL,
            context={"var": VAR_LOG_LEVEL, "value": raw},
        )
    return level


def _parse_inputs(env: Mapping[str, str]) -> tuple[Path, ...]:
    raw = env.get(VAR_INPUTS, "").strip()
    if not raw:
        raise ConfigError(
            f"{VAR_INPUTS} is required for merge "
            f"(ordered file paths separated by {INPUTS_SEPARATOR!r})",
            error_code=ErrorCode.MISSING_VAR,
            context={"var": VAR_INPUTS},
        )
    parts = [part.strip() for part in raw.split(INPUTS_SEPARATOR)]
    if any(not part for part in parts):
        raise ConfigError(
            f"{VAR_INPUTS} contains an empty path component "
            f"(check for stray {INPUTS_SEPARATOR!r} separators)",
            error_code=ErrorCode.INVALID_INPUTS,
            context={"var": VAR_INPUTS, "value": raw},
        )
    paths = [Path(part) for part in parts]
    # Duplicates are detected on the parsed Path objects, not the raw strings:
    # '/in/a.pdf', '/in/./a.pdf' and '/in//a.pdf' are the same file spelled
    # three ways, and a repeated merge input is almost always a templating bug
    # that would silently duplicate content in the output document. (Aliasing
    # through symlinks can't be caught here - config parsing stays
    # filesystem-free by design.)
    duplicated_paths = {p for p in paths if paths.count(p) > 1}
    if duplicated_paths:
        duplicates = sorted({part for part in parts if Path(part) in duplicated_paths})
        raise ConfigError(
            f"{VAR_INPUTS} lists the same path more than once: {', '.join(duplicates)}",
            error_code=ErrorCode.DUPLICATE_INPUTS,
            context={"var": VAR_INPUTS, "duplicates": duplicates},
        )
    return tuple(paths)


def _parse_output(env: Mapping[str, str]) -> Path:
    raw = env.get(VAR_OUTPUT, "").strip()
    if not raw:
        raise ConfigError(
            f"{VAR_OUTPUT} is required for merge (path of the output PDF)",
            error_code=ErrorCode.MISSING_VAR,
            context={"var": VAR_OUTPUT},
        )
    return Path(raw)


def _parse_single_path(env: Mapping[str, str], var: str, purpose: str) -> Path:
    raw = env.get(var, "").strip()
    if not raw:
        raise ConfigError(
            f"{var} is required for extract ({purpose})",
            error_code=ErrorCode.MISSING_VAR,
            context={"var": var},
        )
    return Path(raw)


def _parse_flag(env: Mapping[str, str], var: str, *, default: bool = False) -> bool:
    raw = env.get(var, "").strip()
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in ("true", "false"):
        return normalized == "true"
    raise ConfigError(
        f"{var} has invalid value {raw!r} (accepted values: true, false, case-insensitive)",
        error_code=ErrorCode.INVALID_FLAG,
        context={"var": var, "value": raw},
    )


def _parse_secret_pair(env: Mapping[str, str], value_var: str, file_var: str) -> SecretRef | None:
    """One secret, two mutually exclusive channels: direct value or file path.

    The direct value is taken verbatim (passwords may legitimately begin or
    end with whitespace); an empty value counts as unset, consistent with the
    empty-equals-missing rule.
    """
    raw_value = env.get(value_var, "")
    raw_file = env.get(file_var, "").strip()
    if raw_value and raw_file:
        raise ConfigError(
            f"{value_var} and {file_var} are mutually exclusive - supply one",
            error_code=ErrorCode.CONFLICTING_PASSWORD_SOURCES,
            context={"vars": [value_var, file_var]},
        )
    if raw_value:
        return EnvSecret(value=Secret(raw_value))
    if raw_file:
        return FileSecret(path=Path(raw_file))
    return None


def _parse_on_exists(env: Mapping[str, str]) -> OnExists:
    raw = env.get(VAR_ON_EXISTS, "").strip()
    if not raw:
        return OnExists.FAIL
    try:
        return OnExists(raw.lower())
    except ValueError:
        raise ConfigError(
            f"{VAR_ON_EXISTS} has invalid value {raw!r} "
            "(accepted values: fail, overwrite, skip, case-insensitive)",
            error_code=ErrorCode.INVALID_ON_EXISTS,
            context={"var": VAR_ON_EXISTS, "value": raw},
        ) from None


def _parse_output_encryption(env: Mapping[str, str]) -> OutputEncryption:
    raw = env.get(VAR_OUTPUT_ENCRYPTION, "").strip()
    if not raw:
        return OutputEncryption.NEVER
    try:
        return OutputEncryption(raw.lower())
    except ValueError:
        raise ConfigError(
            f"{VAR_OUTPUT_ENCRYPTION} has invalid value {raw!r} "
            "(accepted values: never, inherit, always, case-insensitive)",
            error_code=ErrorCode.INVALID_OUTPUT_ENCRYPTION,
            context={"var": VAR_OUTPUT_ENCRYPTION, "value": raw},
        ) from None


def _parse_output_password(env: Mapping[str, str], password: SecretRef | None) -> SecretRef | None:
    output_password = _parse_secret_pair(env, VAR_OUTPUT_PASSWORD, VAR_OUTPUT_PASSWORD_FILE)
    mode = _parse_output_encryption(env)
    if mode is OutputEncryption.NEVER and output_password is not None:
        # An output password that can never be used is config drift of the
        # same kind as a typo'd variable: fail loudly, don't ignore.
        raise ConfigError(
            f"an output password is supplied but {VAR_OUTPUT_ENCRYPTION} is 'never' "
            "(set it to 'inherit' or 'always', or remove the output password)",
            error_code=ErrorCode.OUTPUT_PASSWORD_WITHOUT_ENCRYPTION,
            context={"var": VAR_OUTPUT_ENCRYPTION},
        )
    if mode is OutputEncryption.ALWAYS and output_password is None and password is None:
        raise ConfigError(
            f"{VAR_OUTPUT_ENCRYPTION}=always requires an output password "
            f"({VAR_OUTPUT_PASSWORD_FILE}/{VAR_OUTPUT_PASSWORD}) or an input password to fall back to",
            error_code=ErrorCode.MISSING_OUTPUT_PASSWORD,
            context={"var": VAR_OUTPUT_ENCRYPTION},
        )
    return output_password
