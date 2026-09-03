"""The JSON log line shape is an operator interface - pin it."""

import json
import logging
import sys

import pytest

from pdf_ops.logging_setup import (
    JsonFormatter,
    _ThirdPartyEventFilter,
    emit_terminal,
    setup_logging,
)
from tests.helpers import make_record

pytestmark = pytest.mark.unit


class TestJsonFormatter:
    def test_emits_valid_json_with_required_fields(self) -> None:
        payload = json.loads(JsonFormatter().format(make_record()))
        assert payload["event"] == "some_event"
        assert payload["level"] == "info"
        # ISO-8601 UTC timestamp
        assert payload["ts"].endswith("+00:00")

    def test_extra_fields_are_merged_into_payload(self) -> None:
        record = make_record(operation="merge", exit_code=2, context={"a": 1})
        payload = json.loads(JsonFormatter().format(record))
        assert payload["operation"] == "merge"
        assert payload["exit_code"] == 2
        assert payload["context"] == {"a": 1}

    def test_exception_info_is_included(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = make_record(exc_info=sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        assert payload["exc_type"] == "ValueError"
        assert "boom" in payload["traceback"]

    def test_unserializable_values_fall_back_to_str(self) -> None:
        record = make_record(path=object())
        payload = json.loads(JsonFormatter().format(record))
        assert isinstance(payload["path"], str)


class TestSetupLogging:
    def test_returns_configured_logger_writing_json_lines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = setup_logging(logging.INFO)
        logger.info("hello_event", extra={"k": "v"})
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["event"] == "hello_event"
        assert payload["k"] == "v"

    def test_repeated_setup_does_not_duplicate_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        setup_logging()
        logger = setup_logging()
        logger.info("once_event")
        assert capsys.readouterr().out.count("once_event") == 1

    def test_level_filters_events(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = setup_logging(logging.ERROR)
        logger.info("quiet_event")
        logger.error("loud_event")
        out = capsys.readouterr().out
        assert "quiet_event" not in out
        assert "loud_event" in out


class TestEmitTerminal:
    def test_bypasses_level_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The terminal event is the workflow engine's only completion signal;
        # PDFOPS_LOG_LEVEL must never be able to suppress it.
        logger = setup_logging(logging.ERROR)
        emit_terminal(logger, logging.INFO, "operation_complete", {"exit_code": 0})
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["event"] == "operation_complete"
        assert payload["level"] == "info"
        assert payload["exit_code"] == 0

    def test_includes_exception_info_when_requested(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = setup_logging(logging.INFO)
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            emit_terminal(
                logger,
                logging.ERROR,
                "operation_failed",
                {"exit_code": 1},
                include_exc_info=True,
            )
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["exc_type"] == "RuntimeError"
        assert "kaboom" in payload["traceback"]


class TestThirdPartyEventFilter:
    def pypdf_record(self, message: str) -> logging.LogRecord:
        return make_record(message, name="pypdf", level=logging.WARNING)

    def test_saslprep_codepoints_are_masked(self) -> None:
        # pypdf's SASLprep warning names the exact codepoint of a password
        # character. Config-level rejection of control characters makes this
        # path unreachable today; the mask stays as defense in depth and is
        # pinned here directly.
        record = self.pypdf_record("stripping non-SASLprep character U+0301 from password")
        assert _ThirdPartyEventFilter().filter(record) is True
        payload = json.loads(JsonFormatter().format(record))
        assert payload["event"] == "pdf_library_message"
        assert payload["source"] == "pypdf"
        assert "U+0301" not in json.dumps(payload)
        assert "U+****" in str(payload["detail"])

    def test_non_saslprep_message_passes_through_verbatim(self) -> None:
        record = self.pypdf_record("Object 9 0 not defined.")
        assert _ThirdPartyEventFilter().filter(record) is True
        payload = json.loads(JsonFormatter().format(record))
        assert payload["event"] == "pdf_library_message"
        assert payload["detail"] == "Object 9 0 not defined."
