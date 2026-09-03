"""The whole secret lifecycle in one place.

Four layers, one module:

1. ``Secret`` - a wrapper that cannot leak through repr, str, or f-strings;
   the raw value is reachable only through ``reveal()``. Its callers are
   this module (validation and scrub registration) and the engine's
   decrypt/encrypt calls - nothing else.
2. ``EnvSecret``/``FileSecret`` - parse-time references to where a secret
   comes from. The config parser stays filesystem-free by capturing the
   source, not the value.
3. ``resolve_secret``/``resolve_and_register`` - the one place secret file
   I/O happens, deliberately late: merge's skip short-circuit never resolves
   at all, so a retry after success works even when the mounted password
   file is already gone.
4. Registration - resolved values are handed to the logging layer's
   defense-in-depth scrubber (the structural wrapper is the primary
   guarantee; scrubbing catches library residue such as exception messages).
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from pdf_ops.errors import ConfigError, ErrorCode
from pdf_ops.logging_setup import register_secret_value


class Secret:
    """A string that renders as ``***`` everywhere; ``reveal()`` is the only
    way to the value, which keeps accidental leakage a type error rather
    than a code-review hope."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    def __str__(self) -> str:
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True, slots=True)
class EnvSecret:
    """A secret supplied directly in an environment variable."""

    value: Secret

    def describe(self) -> str:
        return "set(env)"

    def resolve(self) -> Secret:
        _reject_control_characters(self.value.reveal(), "the environment")
        return self.value


@dataclass(frozen=True, slots=True)
class FileSecret:
    """A secret to be read from a mounted file (resolved after parsing -
    the parser itself stays filesystem-free)."""

    path: Path

    def describe(self) -> str:
        return "set(file)"

    def resolve(self) -> Secret:
        """Read the file; a single trailing newline is stripped (hand-created
        secret files usually have one; the password itself is otherwise taken
        byte-for-byte)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as err:
            raise ConfigError(
                f"cannot read password file {self.path}: {err.strerror or err}",
                error_code=ErrorCode.PASSWORD_FILE_UNREADABLE,
                context={"path": str(self.path)},
            ) from err
        except UnicodeDecodeError as err:
            # Deliberately no decode detail: it would name a byte of the
            # secret and its position.
            raise ConfigError(
                f"password file {self.path} is not valid UTF-8 text",
                error_code=ErrorCode.PASSWORD_FILE_UNREADABLE,
                context={"path": str(self.path)},
            ) from err
        raw = raw.removesuffix("\n").removesuffix("\r")
        if not raw:
            raise ConfigError(
                f"password file {self.path} is empty",
                error_code=ErrorCode.EMPTY_PASSWORD,
                context={"path": str(self.path)},
            )
        _reject_control_characters(raw, str(self.path))
        return Secret(raw)


type SecretRef = EnvSecret | FileSecret


def describe_secret(ref: SecretRef | None) -> str:
    """Presence-only description for the config-echo log event."""
    return "unset" if ref is None else ref.describe()


def resolve_secret(ref: SecretRef | None) -> Secret | None:
    """Materialize a secret reference; raises ConfigError on unreadable,
    non-UTF-8, or empty files and on control characters."""
    return None if ref is None else ref.resolve()


@dataclass(frozen=True, slots=True)
class Secrets:
    """Materialized secrets for one run, resolved from the config's refs."""

    password: Secret | None
    output_password: Secret | None


def resolve_and_register(
    password: SecretRef | None,
    output_password: SecretRef | None,
    logger: logging.Logger,
) -> Secrets:
    """Materialize both secrets and wire them into log scrubbing.

    The input password resolves first, deliberately: when both sources are
    bad, the failure event names the primary secret's problem.
    """
    secrets = Secrets(
        password=resolve_secret(password),
        output_password=resolve_secret(output_password),
    )
    for secret in (secrets.password, secrets.output_password):
        if secret is not None and not register_secret_value(secret.reveal()):
            logger.warning(
                "redaction_degraded",
                extra={
                    "detail": "a supplied secret is too short for defense-in-depth "
                    "log scrubbing; the structural no-leak layers still apply"
                },
            )
    return secrets


def _reject_control_characters(value: str, origin: str) -> None:
    """A password containing control characters is almost certainly an
    encoding or copy-paste accident - and downstream cryptographic
    normalization (SASLprep) would warn about it, naming the codepoint."""
    if any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise ConfigError(
            f"the password from {origin} contains control characters "
            "(check for encoding or copy-paste issues)",
            error_code=ErrorCode.PASSWORD_UNSUPPORTED_CHARACTERS,
            context={"source": origin},
        )
