# pdf-ops

Containerized PDF operations (merge, extract attachments) for workflow engines
such as Argo. One operation per container run, configured only by `PDFOPS_*`
environment variables; the exit code and the JSON log stream are the API.

## Read first

- `docs/DESIGN.md` - the architecture and the reasons behind it: module table,
  the engine seam, the security posture, known limitations.
- `docs/OPERATIONS.md` - the operator contract: paths, passwords, output policy,
  log events, and the complete error-code table.
- `docs/DECISIONS.md` - the decision register. Every architectural choice has a
  `D-###` entry with its alternatives and status.
- `docs/diagrams/index.html` - interactive maps of the architecture, the retry
  lifecycle, the extract trust boundary and the password flow.
- `deploy/argo-example.yaml` - the full deployment posture the image is tested
  against.

## Gates

All of these must pass before a commit; CI runs the same commands.

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
uv run python docs/scripts/validate_decisions.py docs/DECISIONS.md
uv run pytest -m container   # needs Docker; run when the Dockerfile or entrypoint changes
```

Run `uv run pre-commit install` once; the hooks run the same tools through uv,
so there is exactly one version of each, pinned in `uv.lock`.

## Conventions

- Contracts are typed and tested: exit codes live in `ExitCode`, error codes in
  `ErrorCode` (a test keeps them in sync with the table in `docs/OPERATIONS.md`),
  and every policy value is an enum or a `Literal` alias, never a bare string.
- `config.py` is filesystem-free. Secrets are captured as references and
  materialized lazily in `secrets.py`, the one place secret file I/O happens.
- `engine_pikepdf.py` is the only module that imports pikepdf; library failures
  are translated into the error taxonomy there and nowhere else. pypdf is a
  dev-only test oracle: fixtures are built with it, outputs verified with it.
- Every module docstring says what the module owns and why. Comments explain
  reasoning, not mechanics, and the voice is the same in code, tests and docs.
- A new architectural choice gets a `D-###` entry (format in
  `docs/DECISION_TRACKING_STANDARD.md`); CI validates the register.
- User-visible changes go in `CHANGELOG.md` under Unreleased.
