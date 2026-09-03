"""Top-level orchestration: the single error boundary and operation dispatch."""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from pdf_ops.config import Config, ExtractConfig, MergeConfig, parse_config
from pdf_ops.errors import ErrorCode, ExitCode, PdfOpsError
from pdf_ops.extract import run_extract
from pdf_ops.logging_setup import emit_terminal, setup_logging
from pdf_ops.merge import run_merge
from pdf_ops.secrets import Secrets, describe_secret, resolve_and_register


def run(env: Mapping[str, str]) -> int:
    """Execute the one operation described by ``env``; return the exit code.

    This is the application's only error boundary: every predictable failure
    is a PdfOpsError carrying its exit code and error code; anything else
    exits UNEXPECTED (1) with a logged traceback. No other module logs-and-
    swallows or exits. Terminal events are emitted through ``emit_terminal``
    so PDFOPS_LOG_LEVEL can never suppress them.
    """
    logger = setup_logging()
    started = time.monotonic()
    try:
        config = parse_config(env)
        logger.setLevel(config.log_level)

        def get_secrets() -> Secrets:
            # Resolved lazily: merge's skip short-circuit must succeed even
            # when the mounted password file is already gone - a retry after
            # success reads nothing at all.
            output_ref = config.output_password if isinstance(config, MergeConfig) else None
            return resolve_and_register(config.password, output_ref, logger)

        logger.info("config_loaded", extra=_config_echo(config))
        result = _dispatch(config, get_secrets, logger)
        emit_terminal(
            logger,
            logging.INFO,
            "operation_complete",
            {
                "operation": config.operation.value,
                "exit_code": int(ExitCode.SUCCESS),
                "duration_s": round(time.monotonic() - started, 3),
                **(result or {}),
            },
        )
        return int(ExitCode.SUCCESS)
    except PdfOpsError as err:
        emit_terminal(
            logger,
            logging.ERROR,
            "operation_failed",
            {
                "error_code": err.error_code,
                "error_message": err.message,
                "exit_code": int(err.exit_code),
                "context": err.context,
                "duration_s": round(time.monotonic() - started, 3),
            },
        )
        return int(err.exit_code)
    except Exception:
        emit_terminal(
            logger,
            logging.ERROR,
            "operation_failed",
            {
                "error_code": ErrorCode.UNEXPECTED_ERROR,
                "exit_code": int(ExitCode.UNEXPECTED),
                "duration_s": round(time.monotonic() - started, 3),
            },
            include_exc_info=True,
        )
        return int(ExitCode.UNEXPECTED)


def _config_echo(config: Config) -> dict[str, Any]:
    """The config_loaded payload: secrets appear as presence only, never value."""
    echo: dict[str, Any] = {
        "operation": config.operation.value,
        "log_level": logging.getLevelName(config.log_level).lower(),
        "password": describe_secret(config.password),
        "on_exists": config.on_exists.value,
    }
    if isinstance(config, MergeConfig):
        echo["output_encryption"] = config.output_encryption.value
        echo["output_password"] = describe_secret(config.output_password)
    return echo


def _dispatch(
    config: Config, get_secrets: Callable[[], Secrets], logger: logging.Logger
) -> dict[str, Any] | None:
    logger.info("operation_started", extra={"operation": config.operation.value})
    match config:
        case MergeConfig():
            return run_merge(config, get_secrets, logger)
        case ExtractConfig():
            return run_extract(config, get_secrets(), logger)
