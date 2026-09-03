"""In-process end-to-end tests through run(env): the cross-operation contract.

Each test asserts what a workflow engine actually observes: the exit code and
the JSON events on stdout. The ``run_app`` fixture (tests/conftest.py) also
enforces on every run: nothing on stderr, every line valid JSON, and exactly
one terminal event, emitted last.
"""

import logging
from typing import Any

import pytest

import pdf_ops.main
from tests.helpers import RunApp

pytestmark = pytest.mark.integration


class TestConfigFailures:
    def test_missing_operation_exits_2(self, run_app: RunApp) -> None:
        code, events = run_app({})
        assert code == 2
        assert [e["event"] for e in events] == ["operation_failed"]
        terminal = events[-1]
        assert terminal["error_code"] == "MISSING_VAR"
        assert terminal["exit_code"] == 2
        assert terminal["level"] == "error"

    def test_invalid_operation_exits_2(self, run_app: RunApp) -> None:
        code, events = run_app({"PDFOPS_OPERATION": "bogus"})
        assert code == 2
        assert [e["event"] for e in events] == ["operation_failed"]
        assert events[-1]["error_code"] == "INVALID_OPERATION"
        assert "bogus" in events[-1]["error_message"]

    def test_unknown_var_typo_exits_2(self, run_app: RunApp) -> None:
        # The typo check runs before anything else - even before required-var
        # checks - so a misspelling is reported as what it is.
        env = {"PDFOPS_OPERATION": "merge", "PDFOPS_OPERATIONN": "merge"}
        code, events = run_app(env)
        assert code == 2
        assert [e["event"] for e in events] == ["operation_failed"]
        assert events[-1]["error_code"] == "UNKNOWN_VAR"


class TestSuccessContract:
    """Pins the success shape of the run() boundary independent of any real
    PDF work, by stubbing the dispatcher."""

    @pytest.fixture
    def successful_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_dispatch(config: Any, secrets: Any, logger: logging.Logger) -> dict[str, Any]:
            logger.info("operation_started", extra={"operation": config.operation.value})
            return {}

        monkeypatch.setattr(pdf_ops.main, "_dispatch", fake_dispatch)

    def test_success_emits_complete_event_and_exit_0(
        self, successful_dispatch: None, run_app: RunApp
    ) -> None:
        env = {
            "PDFOPS_OPERATION": "merge",
            "PDFOPS_INPUTS": "/in/a.pdf",
            "PDFOPS_OUTPUT": "/out/m.pdf",
        }
        code, events = run_app(env)
        assert code == 0
        assert [e["event"] for e in events] == [
            "config_loaded",
            "operation_started",
            "operation_complete",
        ]
        terminal = events[-1]
        assert terminal["exit_code"] == 0
        assert terminal["operation"] == "merge"
        assert terminal["level"] == "info"


class TestUnexpectedErrorBoundary:
    """A dispatcher stub raising a plain exception pins the unexpected-error
    boundary (exit 1 with a logged traceback) independent of any real
    operation code."""

    @pytest.fixture
    def crashing_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_dispatch(config: Any, secrets: Any, logger: logging.Logger) -> dict[str, Any]:
            logger.info("operation_started", extra={"operation": config.operation.value})
            raise RuntimeError("simulated internal bug")

        monkeypatch.setattr(pdf_ops.main, "_dispatch", fake_dispatch)

    def test_unexpected_exception_exits_1_with_traceback(
        self, crashing_dispatch: None, run_app: RunApp
    ) -> None:
        env = {
            "PDFOPS_OPERATION": "merge",
            "PDFOPS_INPUTS": "/in/a.pdf",
            "PDFOPS_OUTPUT": "/out/m.pdf",
        }
        code, events = run_app(env)
        assert code == 1
        assert [e["event"] for e in events] == [
            "config_loaded",
            "operation_started",
            "operation_failed",
        ]
        terminal = events[-1]
        assert terminal["error_code"] == "UNEXPECTED_ERROR"
        assert terminal["exit_code"] == 1
        assert terminal["exc_type"] == "RuntimeError"
        assert "traceback" in terminal
        assert isinstance(terminal["duration_s"], float)

    def test_log_level_error_silences_lifecycle_events(
        self, crashing_dispatch: None, run_app: RunApp
    ) -> None:
        env = {
            "PDFOPS_OPERATION": "merge",
            "PDFOPS_INPUTS": "/in/a.pdf",
            "PDFOPS_OUTPUT": "/out/m.pdf",
            "PDFOPS_LOG_LEVEL": "error",
        }
        code, events = run_app(env)
        assert code == 1
        assert [e["event"] for e in events] == ["operation_failed"]

    def test_config_error_before_level_applies_still_logged(self, run_app: RunApp) -> None:
        # A config failure happens before the requested level is applied; the
        # failure event must still appear (bootstrap level is INFO).
        env = {"PDFOPS_OPERATION": "merge", "PDFOPS_LOG_LEVEL": "nonsense"}
        code, events = run_app(env)
        assert code == 2
        assert events[-1]["error_code"] == "INVALID_LOG_LEVEL"
