"""The Secret wrapper and the log-redaction layer - the no-leak machinery."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Generator
from typing import Any

import pytest

from pdf_ops.logging_setup import (
    JsonFormatter,
    clear_registered_secrets,
    register_secret_value,
)
from pdf_ops.secrets import Secret
from tests.helpers import make_record

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_registry() -> Generator[None]:
    # The redaction registry is process-global; every test starts and ends empty.
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def format_record(msg: str, **extra: Any) -> dict[str, object]:
    return json.loads(JsonFormatter().format(make_record(msg, level=logging.ERROR, **extra)))


class TestSecret:
    def test_never_leaks_through_repr_str_or_format(self) -> None:
        secret = Secret("hunter2")
        assert repr(secret) == "***"
        assert str(secret) == "***"
        assert f"password is {secret}" == "password is ***"
        assert "hunter2" not in f"{secret!r}{secret!s}"

    def test_bool_reflects_emptiness(self) -> None:
        assert Secret("x")
        assert not Secret("")


class TestRedactionFilter:
    def test_registered_value_scrubbed_from_string_fields(self) -> None:
        register_secret_value("hunter2")
        payload = format_record("an_event", detail="failed with password hunter2 somewhere")
        assert "hunter2" not in json.dumps(payload)
        assert payload["detail"] == "failed with password *** somewhere"

    def test_scrub_recurses_into_context_dicts_and_lists(self) -> None:
        register_secret_value("hunter2")
        payload = format_record("an_event", context={"inputs": ["a hunter2 b"], "note": "hunter2"})
        serialized = json.dumps(payload)
        assert "hunter2" not in serialized
        assert "***" in serialized

    def test_traceback_payloads_are_scrubbed(self) -> None:
        register_secret_value("hunter2")
        try:
            raise RuntimeError("boom hunter2")
        except RuntimeError:
            payload = format_record("operation_failed", exc_info=sys.exc_info())
        assert "hunter2" not in json.dumps(payload)
        assert "boom ***" in str(payload["traceback"])


class TestScrubIntegrity:
    def test_token_fields_never_scrubbed(self) -> None:
        # A password equal to a known token ("merge") must not rewrite
        # code-controlled fields: doing so both breaks workflow-engine
        # matching and acts as a password oracle.
        register_secret_value("merge")
        payload = format_record("merge_written", operation="merge", error_code="MERGE_FAILED")
        assert payload["event"] == "merge_written"
        assert payload["operation"] == "merge"
        assert payload["error_code"] == "MERGE_FAILED"

    def test_free_text_fields_are_scrubbed(self) -> None:
        register_secret_value("merge")
        payload = format_record("merge_written", detail="library said merge is wrong")
        assert payload["detail"] == "library said *** is wrong"

    def test_overlapping_secrets_scrub_longest_first(self) -> None:
        register_secret_value("Spring2026")
        register_secret_value("Spring2026!x9")
        payload = format_record("merge_written", detail="bad key 'Spring2026!x9' rejected")
        assert payload["detail"] == "bad key '***' rejected"
        assert "!x9" not in str(payload["detail"])

    def test_repr_escaped_variant_also_scrubbed(self) -> None:
        register_secret_value("back\\slash-pw")
        # a library embedding the value via %r doubles the backslash
        payload = format_record("merge_written", detail="rejected 'back\\\\slash-pw' here")
        assert "slash-pw" not in str(payload["detail"])

    def test_too_short_secrets_are_not_registered(self) -> None:
        assert register_secret_value("abc") is False
        payload = format_record("merge_written", detail="abc appears here")
        assert payload["detail"] == "abc appears here"
