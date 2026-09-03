"""The merge operation: validate everything, then write once, atomically."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from pdf_ops.config import MergeConfig, OutputEncryption
from pdf_ops.engine import OpenedInput, get_engine
from pdf_ops.errors import ConfigError, ErrorCode
from pdf_ops.inputs import validate_inputs
from pdf_ops.output import atomic_output, check_output_path, clean_stale_temps
from pdf_ops.secrets import Secret, Secrets

# Where the output password came from, for the output_encrypted event.
type PasswordSource = Literal["output", "input-fallback"]


def run_merge(
    config: MergeConfig, get_secrets: Callable[[], Secrets], logger: logging.Logger
) -> dict[str, Any]:
    action = check_output_path(config.output, config.on_exists)
    if action == "skip":
        # The strongest idempotency semantics: an existing output means the
        # work is done - neither the inputs nor a mounted password file are
        # read (a retry after success must succeed even if upstream
        # artifacts are already gone).
        logger.info("output_skipped", extra={"output_path": str(config.output)})
        return {"skipped": True, "output_path": str(config.output)}
    for stale_name in clean_stale_temps(config.output):
        logger.warning("stale_temp_removed", extra={"temp_file": stale_name})

    validate_inputs(config.inputs)
    secrets = get_secrets()

    engine = get_engine()
    opened: list[OpenedInput] = []
    for path in config.inputs:
        one = engine.open_input(path, secrets.password)
        opened.append(one)
        logger.info("input_opened", extra=one.event_fields())
        for message in one.warnings:
            logger.warning("pdf_library_message", extra={"detail": message, "source": "qpdf"})

    encrypted_count = sum(1 for one in opened if one.encrypted)
    output_password, password_source = _choose_output_password(config, secrets, encrypted_count)
    if (
        secrets.password is not None
        and encrypted_count == 0
        and password_source != "input-fallback"
    ):
        # Not warned when the input password was consumed as the
        # output-encryption fallback - it was used, just not for decryption.
        logger.warning(
            "password_unused",
            extra={"detail": "a password was supplied but no input is encrypted"},
        )
    if output_password is None and encrypted_count > 0:
        # never-mode with encrypted inputs: the merge proceeds, but the
        # confidentiality downgrade must be impossible to miss in the log.
        logger.warning(
            "security_downgrade",
            extra={
                "encrypted_inputs": encrypted_count,
                "detail": "encrypted input(s) merged into an unencrypted output "
                "(PDFOPS_OUTPUT_ENCRYPTION=never)",
            },
        )

    with atomic_output(config.output) as tmp_path:
        late_warnings = engine.merge_to(opened, tmp_path, output_password)
    for message in late_warnings:
        # sources are read lazily, so repairs can surface at write time
        logger.warning("pdf_library_message", extra={"detail": message, "source": "qpdf"})
    if action == "overwrite":
        logger.info("output_overwritten", extra={"output_path": str(config.output)})

    if output_password is not None:
        logger.info(
            "output_encrypted",
            extra={"algorithm": "AES-256", "password_source": password_source},
        )
    logger.info(
        "merge_written",
        extra={
            "output_path": str(config.output),
            "pages_per_input": [one.pages for one in opened],
            "output_encrypted": output_password is not None,
        },
    )
    return {
        "inputs_merged": len(config.inputs),
        "pages": sum(one.pages for one in opened),
        "bytes_written": config.output.stat().st_size,
        "output_path": str(config.output),
        "output_encrypted": output_password is not None,
    }


def _choose_output_password(
    config: MergeConfig, secrets: Secrets, encrypted_count: int
) -> tuple[Secret | None, PasswordSource | None]:
    """Apply the output-encryption policy; returns (password, source-label).

    The fallback to the input password uses only an *explicitly supplied*
    one - inputs opened via the empty auto-try carry no real secret, and
    encrypting the output with an empty password would be a lock made of
    paper.
    """
    mode = config.output_encryption
    if mode is OutputEncryption.NEVER:
        return None, None
    if mode is OutputEncryption.INHERIT and encrypted_count == 0:
        return None, None
    if secrets.output_password is not None:
        return secrets.output_password, "output"
    if secrets.password is not None:
        return secrets.password, "input-fallback"
    raise ConfigError(
        f"output encryption is required (PDFOPS_OUTPUT_ENCRYPTION={mode.value}) but no "
        "explicit password is available - the encrypted input(s) opened with the empty "
        "password; supply PDFOPS_OUTPUT_PASSWORD_FILE or PDFOPS_OUTPUT_PASSWORD",
        error_code=ErrorCode.MISSING_OUTPUT_PASSWORD,
        context={"output_encryption": mode.value},
    )
