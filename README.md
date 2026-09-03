# pdf-ops

[![CI](https://github.com/Radko-D/python-pdfops/actions/workflows/ci.yml/badge.svg)](https://github.com/Radko-D/python-pdfops/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Containerized PDF operations for workflow systems (e.g. Argo Workflows): exactly one
operation per container run - **merge** multiple PDFs into one, or **extract** the
attachments embedded in a PDF - configured entirely through environment variables.

The design - architecture, library tradeoffs, security posture, limitations - is
summarized in [`docs/DESIGN.md`](docs/DESIGN.md); the working notes behind it are
[`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md), and individual choices, with their
alternatives and status, live in the decision register at
[`docs/DECISIONS.md`](docs/DECISIONS.md).

## Quick start

```sh
docker build -t pdf-ops .

# The container runs as UID 10001 - the output dir must be writable by it
mkdir -p in out secret && chmod 777 out

# No PDFs at hand? Conjure the inputs the examples below use:
uv sync && uv run python - <<'PY'
from pypdf import PdfWriter
for name, pages in (("a.pdf", 1), ("b.pdf", 2)):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=300)
    with open(f"in/{name}", "wb") as h:
        w.write(h)
w = PdfWriter(); w.add_blank_page(width=200, height=300)
w.add_attachment("data.csv", b"x,y\n1,2\n")
with open("in/report.pdf", "wb") as h:
    w.write(h)
w = PdfWriter(); w.add_blank_page(width=200, height=300)
w.encrypt(user_password="s3cret-pw", algorithm="AES-256")
with open("in/locked.pdf", "wb") as h:
    w.write(h)
open("secret/pw", "w").write("s3cret-pw\n")
PY

# Merge two PDFs from a mounted input dir into a mounted output dir
docker run --rm \
  -v "$PWD/in:/in:ro" -v "$PWD/out:/out" \
  -e PDFOPS_OPERATION=merge \
  -e PDFOPS_INPUTS=/in/a.pdf:/in/b.pdf \
  -e PDFOPS_OUTPUT=/out/merged.pdf \
  pdf-ops

# Extract the attachments embedded in a PDF
docker run --rm \
  -v "$PWD/in:/in:ro" -v "$PWD/out:/out" \
  -e PDFOPS_OPERATION=extract \
  -e PDFOPS_INPUT=/in/report.pdf \
  -e PDFOPS_OUTPUT_DIR=/out \
  pdf-ops

# Merge an encrypted PDF, re-encrypting the output with the same password
docker run --rm \
  -v "$PWD/in:/in:ro" -v "$PWD/out:/out" -v "$PWD/secret:/secret:ro" \
  -e PDFOPS_OPERATION=merge \
  -e PDFOPS_INPUTS=/in/locked.pdf:/in/a.pdf \
  -e PDFOPS_OUTPUT=/out/merged.pdf \
  -e PDFOPS_PASSWORD_FILE=/secret/pw \
  -e PDFOPS_OUTPUT_ENCRYPTION=inherit \
  pdf-ops

# Invalid configuration fails fast (exit 2, machine-readable error event)
docker run --rm -e PDFOPS_OPERATION=bogus pdf-ops
```

The container needs no arguments and no interactive input: behavior comes entirely
from `PDFOPS_*` variables, and the mounted volumes provide inputs and receive outputs.

## Environment variables

| Variable | Operation | Required | Accepted values | Default |
|---|---|---|---|---|
| `PDFOPS_OPERATION` | - | yes | `merge`, `extract` | - |
| `PDFOPS_INPUTS` | merge | yes | ordered list of file paths, `:`-separated (explicit order - no globs) | - |
| `PDFOPS_OUTPUT` | merge | yes | path of the output PDF; its directory must exist | - |
| `PDFOPS_INPUT` | extract | yes | the PDF to extract attachments from | - |
| `PDFOPS_OUTPUT_DIR` | extract | yes | existing directory receiving the attachment files | - |
| `PDFOPS_FAIL_ON_NO_ATTACHMENTS` | extract | no | `true`, `false` (case-insensitive) - fail (exit 3) when the PDF has no attachments | `false` |
| `PDFOPS_PASSWORD_FILE` | both | no | path to a mounted secret file holding the password (preferred channel; one trailing newline stripped) | - |
| `PDFOPS_PASSWORD` | both | no | the password itself - discouraged: env values leak via `kubectl describe`, `/proc/<pid>/environ`, crash tooling | - |
| `PDFOPS_OUTPUT_ENCRYPTION` | merge | no | `never`, `inherit`, `always` (case-insensitive) - see below | `never` |
| `PDFOPS_OUTPUT_PASSWORD_FILE` | merge | no | secret file holding the password for the merged output | - |
| `PDFOPS_OUTPUT_PASSWORD` | merge | no | output password as a direct value (same caveats as `PDFOPS_PASSWORD`) | - |
| `PDFOPS_ON_EXISTS` | both | no | `fail`, `overwrite`, `skip` (case-insensitive) - see Retries | `fail` |
| `PDFOPS_LOG_LEVEL` | - | no | `debug`, `info`, `warning`, `error` (case-insensitive) | `info` |

Strictness rules, all exit 2: any other `PDFOPS_*` variable is rejected as a probable
typo (`UNKNOWN_VAR`); a variable belonging to the other operation is rejected
(`INAPPLICABLE_VAR`); duplicate merge inputs are rejected (`DUPLICATE_INPUTS`) - a
repeated path is almost always a templating bug that would silently duplicate content.

## Path conventions and output behavior

The full behavior contract - mounts and permissions, password semantics, output
encryption, atomic writes, the existing-output policy, attachment-name safety, and
resource sizing -
lives in [`docs/OPERATIONS.md`](docs/OPERATIONS.md). The short version: everything is
mounted volumes with absolute in-container paths, the container runs as non-root UID
10001, outputs are written atomically (a complete file or nothing), and
`PDFOPS_ON_EXISTS` decides what a retry does.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected internal error (traceback in the log) |
| 2 | invalid configuration |
| 3 | input missing/unreadable |
| 4 | invalid or corrupt PDF |
| 5 | password required/wrong/unsupported |
| 6 | output conflict or output location unusable |

The finer-grained `error_code` carried by every `operation_failed` event is listed
per exit code in [`docs/OPERATIONS.md`](docs/OPERATIONS.md#error-codes).

## Logging

Output is JSON lines on stdout - one event per line, stderr stays empty. Lifecycle
events narrate progress and respect `PDFOPS_LOG_LEVEL`; passwords are echoed as
presence only (`unset` / `set(env)` / `set(file)`), never as values. Every run ends
with exactly one terminal event - `operation_complete` or `operation_failed` (with a
machine-readable `error_code`) - which no log level suppresses, so a workflow engine
can always branch on the last line:

```json
{"ts": "2026-09-02T17:55:15.661+00:00", "level": "info", "event": "operation_complete", "operation": "merge", "exit_code": 0, "duration_s": 0.001, "inputs_merged": 2, "pages": 3, "bytes_written": 728, "output_path": "/out/merged.pdf", "output_encrypted": false}
```

The event vocabulary and per-event fields are documented in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Retries

Workflow engines retry at-least-once, and most failures here are deterministic -
retrying a wrong password or a corrupt PDF is pure waste. The retryability of each exit
code:

| Code | Retry? | Why |
|---|---|---|
| 1 | yes | unexpected internal error - the only class where a retry might see different behavior |
| 2 | no | configuration is immutable for a given pod spec |
| 3 | usually no | missing/unreadable input - permanent unless an upstream mount races |
| 4 | no | the PDF itself is bad; it will be bad again |
| 5 | no | the password will still be wrong |
| 6 | usually no | output conflict/location - `DISK_FULL` is the judgment call (space may free up) |

Argo example - retry unexpected errors (exit 1) and pod-level errors, which never
produce an exit code (a lost pod reports "-1"); with `skip` in the environment, a
retry after a lost-but-successful pod is a free no-op:

```yaml
retryStrategy:
  limit: "3"
  expression: >-
    lastRetry.status == "Error" or asInt(lastRetry.exitCode) == 1
# and in the container env:
#   PDFOPS_ON_EXISTS: skip
```

A complete `WorkflowTemplate` - security context, secret-mounted password,
retry strategy, measured resource sizing - lives in
[`deploy/argo-example.yaml`](deploy/argo-example.yaml).

## Development

```sh
uv sync                       # deps + venv
uv run pre-commit install     # once: the hooks run the same tools through uv
uv run pytest                 # unit + integration tests
uv run pytest -m container    # container-contract tests (needs Docker)
uv run ruff check .           # lint
uv run ruff format --check .  # formatting (CI enforces it)
uv run pyright                # strict type check
uv run pre-commit run -a      # full hook chain
```

Changes are recorded in [`CHANGELOG.md`](CHANGELOG.md); conventions for contributors
and coding agents are in [`CLAUDE.md`](CLAUDE.md).
