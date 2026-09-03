"""JSON-lines logging to stdout - the operator interface of the container.

One JSON object per line; a workflow engine (e.g. Argo) captures stdout as the
step log. The log message is a stable machine-readable event token; structured
detail is passed via ``extra`` and merged into the payload.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_LOGGER_NAME = "pdf_ops"

# Attributes present on every LogRecord; anything not listed here arrived via
# ``extra`` and belongs in the JSON payload.
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


# Secret values registered for defense-in-depth scrubbing. The primary rule
# is that secrets are never passed to a log call at all (the Secret wrapper
# makes that structural); this layer catches the residue that rule can't -
# e.g. a library exception whose args embed the password, serialized into a
# traceback payload.
_REDACTED_VALUES: set[str] = set()

# Scrubbing applies ONLY to free-text fields where library residue can land.
# Code-controlled token fields (event, operation, error_code, ...) must stay
# byte-stable: workflow engines branch on them, and rewriting known constants
# would itself disclose the password (seeing "***_written" where
# "merge_written" belongs tells a log reader the password is "merge").
_SCRUBBED_FIELDS = frozenset({"traceback", "detail", "error_message", "context"})

# Below this, scrubbing a secret would shred free text without adding real
# protection; the structural layers still hold, and run() logs a warning.
MIN_SCRUBBED_SECRET_LENGTH = 4


def register_secret_value(value: str) -> bool:
    """Scrub ``value`` from every future log record's free-text fields.

    Also registers the repr-escaped spelling (a library embedding the value
    via ``%r`` writes ``back\\\\slash`` for ``back\\slash``). Returns whether
    the value was long enough to register.
    """
    if len(value) < MIN_SCRUBBED_SECRET_LENGTH:
        return False
    _REDACTED_VALUES.add(value)
    escaped = repr(value)[1:-1]
    if escaped != value:
        _REDACTED_VALUES.add(escaped)
    return True


def clear_registered_secrets() -> None:
    _REDACTED_VALUES.clear()


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        # Longest first: with overlapping secrets registered (e.g. rotated
        # passwords sharing a prefix), replacing the shorter one first would
        # leave the tail of the longer one exposed.
        for secret in sorted(_REDACTED_VALUES, key=len, reverse=True):
            if secret in value:
                value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _scrub(value) if key in _SCRUBBED_FIELDS else value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["traceback"] = _scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def emit_terminal(
    logger: logging.Logger,
    level: int,
    event: str,
    fields: dict[str, Any],
    *,
    include_exc_info: bool = False,
) -> None:
    """Emit a terminal event, bypassing the logger's level filter.

    The operator contract guarantees exactly one terminal event per run
    (``operation_complete`` | ``operation_failed``) regardless of
    PDFOPS_LOG_LEVEL - level filtering applies only to lifecycle and
    diagnostic events. ``Logger.handle`` skips the level check by design.
    """
    exc_info = sys.exc_info() if include_exc_info else None
    record = logger.makeRecord(
        logger.name, level, "(terminal)", 0, event, (), exc_info, extra=fields
    )
    logger.handle(record)


class _ThirdPartyEventFilter(logging.Filter):
    """Rewrite a third-party log record into our event schema.

    The original message moves to ``detail`` and the emitting logger to
    ``source``, so ``event`` stays a stable, greppable token.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        detail = record.getMessage()
        if "SASLprep" in detail:
            # pypdf's SASLprep warning names the exact codepoint of a
            # password character - password material; mask it.
            detail = re.sub(r"U\+[0-9A-Fa-f]{4,6}", "U+****", detail)
        record.detail = detail
        record.source = record.name
        record.msg = "pdf_library_message"
        record.args = ()
        return True


# Loggers whose records must reach stdout as JSON instead of falling through
# to logging.lastResort on stderr: anything the PDF library routes through
# Python logging, and Python warnings (via logging.captureWarnings below).
# Anything on stderr would break the JSON-only/empty-stderr operator
# contract. (qpdf's own parse warnings don't pass through here - the engine
# collects them per input and the operation layer emits them as events.)
_THIRD_PARTY_LOGGERS = ("pikepdf", "py.warnings")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the application logger.

    Replaces any existing handler so the stream always points at the current
    ``sys.stdout`` (keeps repeated in-process runs, and pytest capture, honest).
    Also routes third-party library records and captured warnings into the
    same JSON stream.
    """
    clear_registered_secrets()  # a fresh run registers its own secrets

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    _replace_handlers(logger, handler)

    logging.captureWarnings(True)
    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.setLevel(logging.WARNING)
        third_party.propagate = False
        third_party_handler = logging.StreamHandler(sys.stdout)
        third_party_handler.setFormatter(JsonFormatter())
        third_party_handler.addFilter(_ThirdPartyEventFilter())
        _replace_handlers(third_party, third_party_handler)

    return logger


def _replace_handlers(logger: logging.Logger, handler: logging.Handler) -> None:
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    logger.addHandler(handler)
