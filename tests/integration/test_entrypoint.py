"""The ``python -m pdf_ops`` entrypoint glue, exercised as a real subprocess.

Pins that the module is runnable, that ``os.environ`` (an ``os._Environ``,
not a dict) is accepted by the parsing layer, and that the process exit code
and stdout/stderr split match the documented contract.
"""

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def run_module(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # A minimal, controlled environment: no inherited PDFOPS_* noise.
    env = {"PATH": "/usr/bin:/bin"}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "pdf_ops"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_invalid_config_exits_2_with_json_only_stdout() -> None:
    result = run_module({"PDFOPS_OPERATION": "bogus"})
    assert result.returncode == 2
    assert result.stderr == ""
    lines = result.stdout.strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert events[-1]["event"] == "operation_failed"
    assert events[-1]["error_code"] == "INVALID_OPERATION"


def test_password_env_var_never_reaches_output() -> None:
    result = run_module({"PDFOPS_OPERATION": "bogus", "PDFOPS_PASSWORD": "hunter2-secret"})
    assert result.returncode == 2
    assert "hunter2-secret" not in result.stdout
    assert "hunter2-secret" not in result.stderr


def test_password_scrubbed_from_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # __main__.main snapshots the env, then scrubs secret vars from the live
    # process environment before any work (child processes, crash tooling).
    import os

    from pdf_ops.__main__ import main

    monkeypatch.setenv("PDFOPS_OPERATION", "bogus")
    monkeypatch.setenv("PDFOPS_PASSWORD", "hunter2-secret")
    monkeypatch.setenv("PDFOPS_OUTPUT_PASSWORD", "other-secret")
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "PDFOPS_PASSWORD" not in os.environ
    assert "PDFOPS_OUTPUT_PASSWORD" not in os.environ
