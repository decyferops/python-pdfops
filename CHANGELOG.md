# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). History before this file was
introduced is in the git log.

## [Unreleased]

Review proposal branch `review/toolchain-structure-pydantic`, rebuilt on
`pikepdf-engine` (2026-09-03). Each commit stands alone and passes every gate.

### Added

- `ErrorCode` enum: the complete `error_code` vocabulary in one place, the
  matching table in `docs/OPERATIONS.md`, and a test that fails when the two
  drift in either direction.
- `inputs.py`: up-front input validation shared by merge and extract.
- `CLAUDE.md` (where to read first, the gates, the conventions),
  `SECURITY.md`, `.github/dependabot.yml` (uv, GitHub Actions, Docker), and
  this changelog.
- Ruff rule groups SIM, PTH, PIE, RET, PERF, FURB and N (ASYNC dropped: no
  async code).
- pyproject URLs and classifiers, including `Private :: Do Not Upload` as a
  guard against publishing a container-only project by accident.

### Changed

- **pre-commit** runs ruff and pyright through uv, so hooks, CI and a
  developer's shell use the single version pinned in `uv.lock`.
- **CI**: least-privilege token, per-ref concurrency, job timeouts, actions
  pinned by commit SHA, `uv sync --locked`, and the decision validator run on
  the project interpreter instead of the runner's system Python.
- `check_output_path`'s verdict, `OpenedInput.password_type` and the
  output-password source are `Literal` aliases instead of bare strings.
- pikepdf failure translation for structure walks is stated once in a
  `_translating` context manager; the `input_opened` payload is built by
  `OpenedInput.event_fields()` for both operations.
- Tests: one `make_record` helper and one registry fixture replace four
  hand-rolled record builders and two duplicate fixtures; `RunApp` and the
  raw-PDF builder live in `tests/helpers.py`, so no test imports from
  `conftest.py`.
- `docs/scripts` and `scripts/` are linted and type-checked like the rest of
  the code.
- README development section lists the format check CI enforces and the
  one-time `pre-commit install`.

### Removed

- `from __future__ import annotations` from every module: the package requires
  Python 3.14, where annotations are already deferred.
- A stale `pyright: ignore` comment for a disabled rule; pyright now reports
  an ignore that suppresses nothing.
- The unused `.coverage` entry in `.gitignore`.
- `merge.py` no longer exports `validate_inputs`; import it from `inputs.py`.
