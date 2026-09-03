# Operational behavior

The behavior contract for operators: paths and permissions, password semantics,
output policies, and what the log stream carries. The short reference tables
(environment variables, exit codes, retryability) live in the
[README](../README.md).

## Paths and permissions

- Input files and the output location are **mounted volumes**; use absolute
  in-container paths.
- The container runs as **non-root UID 10001**: input mounts need to be readable and
  the output mount writable by that UID (`chmod`/`chown` for plain Docker; `fsGroup`
  in a pod `securityContext` on Kubernetes).
- Outputs get regular `open()`-style permissions (the container umask, `0644` by
  default), so a later step running as a different user can read them from a shared
  volume.
- The image needs no writable root filesystem: every write lands in the output
  mount, so `readOnlyRootFilesystem: true` (plus dropped capabilities and no
  privilege escalation) works unmodified - a container test runs the golden merge
  under exactly that posture.

## Passwords

- One password (via `PDFOPS_PASSWORD_FILE`, preferably) is tried against every
  encrypted input; owner-only "permissions-locked" PDFs open without a password via
  the spec-standard empty-password try, exactly like every PDF viewer. Wrong
  password -> exit 5 naming the failing input.
- The password itself never appears in any output: the in-process `Secret` wrapper
  renders as `***`, the logging layer scrubs registered secret values (four
  characters or longer; a shorter one draws a `redaction_degraded` warning) from the
  free-text fields of every event including tracebacks, and the process scrubs
  `PDFOPS_PASSWORD` and `PDFOPS_OUTPUT_PASSWORD` from its own environment on startup.
- Each `input_opened` event reports the encryption algorithm (read from the PDF's
  plaintext `/Encrypt` dictionary) and how the file opened (`user`/`owner`/`empty`).
- A permissions-locked input among user-locked ones never fails just because a
  password was supplied: the empty try still applies per input (the exact call
  sequence is drawn in [`diagrams/index.html#passwords`](diagrams/index.html#passwords)).
- Passwords containing control characters are rejected (exit 2) as encoding
  accidents, at the moment the password is resolved - a merge that short-circuits
  on `skip` never reads it.
- Note the env channel's inherent limit: the initial environment block stays visible
  to `docker inspect` and `/proc/<pid>/environ` - the file channel is the one that
  keeps the value out of the process's environment entirely.

## Output encryption

`PDFOPS_OUTPUT_ENCRYPTION`: `never` (default) writes a plaintext output and emits a
loud `security_downgrade` warning when inputs were encrypted; `inherit` encrypts the
output iff at least one input was encrypted - confidentiality never decreases through
this step; `always` encrypts unconditionally. The output password comes from
`PDFOPS_OUTPUT_PASSWORD_FILE`/`PDFOPS_OUTPUT_PASSWORD`, falling back to the
*explicitly supplied* input password (never the empty auto-try). Output encryption is
always AES-256, whatever the inputs used. Supplying an output password while the mode
is `never` is a hard configuration error. With no password available anywhere
(`always` at config parse, or `inherit` when the only encrypted inputs opened via the
empty try) the run fails with `MISSING_OUTPUT_PASSWORD`, exit 2, before anything is
written. `security_downgrade` is a warning-level event, so `PDFOPS_LOG_LEVEL=error`
hides it; the terminal event's `output_encrypted: false` is the level-proof signal.

## Atomic writes and existing outputs

- Merge order is exactly the order of `PDFOPS_INPUTS` - deterministic across retries.
- Output is written **atomically**: work goes to a temp file in the output directory,
  then a single rename. The final path either holds a complete PDF or nothing - a
  failed or killed run never leaves a partial file where a downstream step could
  read it.
- **Existing outputs** follow `PDFOPS_ON_EXISTS`: `fail` (default) refuses with exit
  6; `overwrite` replaces atomically (readers see old bytes or new bytes, never a
  mix); `skip` treats the existing output as a completed prior run. For merge, `skip`
  is a whole-run no-op - exit 0 with `skipped: true`, without even reading the
  inputs. For extract, `skip` is **per-file completion**: only missing attachments
  are written (`attachments_skipped` reports the rest), so a crashed run's partial
  set gets finished by the retry - sound because every file this tool writes is
  atomic and therefore whole. Both `skip` modes trust that an existing file is a
  completed prior output. A directory at the output path, or at any extraction
  target name, is refused under every policy (`OUTPUT_IS_DIRECTORY`, exit 6).
- Temp debris from a crashed prior run (`.name.*.tmp` matching this run's own
  targets) is removed before the first write, with a `stale_temp_removed` event; a
  run refused or failed before that point leaves it for the next attempt. The whole
  run/retry state machine is drawn in
  [`diagrams/index.html#lifecycle`](diagrams/index.html#lifecycle). One writer per
  output path at a time is assumed - which a workflow engine guarantees per step.
- All inputs are validated (existence, readability, PDF header) **before anything is
  written**, and every bad input is reported in a single failure event.

## Attachment extraction

- **Attachment names are treated as untrusted input**: extraction reduces every name
  to a sanitized basename (path separators, traversal segments, and control
  characters removed; deterministic `attachment_<n>` fallback), so a hostile PDF can
  never write outside `PDFOPS_OUTPUT_DIR`. Names longer than 200 bytes are
  truncated. Duplicate names get deterministic `-1`/`-2` suffixes, with collisions
  detected case-insensitively (the volume may be), so `Report.txt` and `report.txt`
  become `Report.txt` and `report-1.txt` on every filesystem; the original name is
  logged whenever sanitization changed it.
- The path every untrusted name travels is drawn in
  [`diagrams/index.html#extract`](diagrams/index.html#extract).
- Extraction order is the PDF's name-tree order - deterministic across runs. Each
  file is written atomically; under the default `fail` policy any pre-existing file
  (or symlink) at a target name refuses the whole run **before** anything is written
  (exit 6) - see `PDFOPS_ON_EXISTS` above for the retry-friendly modes. A directory
  at an output path is refused under every policy (`OUTPUT_IS_DIRECTORY`). A PDF
  with zero attachments is a success with `attachments_extracted=0` unless
  `PDFOPS_FAIL_ON_NO_ATTACHMENTS=true` (exit 3, `NO_ATTACHMENTS`).

## Resource sizing

Measured in-container (`scripts/benchmark.py`, Docker Desktop on an Apple Silicon
host - durations are indicative, the memory profile is structural):

| Scenario | Duration | Peak process RSS | Peak cgroup memory |
|---|---|---|---|
| merge 2 x 5 MB (baseline) | 0.05 s | ~36 MB | ~51 MB |
| merge 2 x 250 MB | 1.8 s | ~529 MB | ~1519 MB |
| merge 20 x 25 MB | 1.2 s | ~531 MB | ~1515 MB |
| merge 250 MB AES-256 in, re-encrypted out | 2.8 s | ~281 MB | ~779 MB |
| extract 10 x 25 MB attachments | 0.5 s | ~333 MB | ~579 MB |

The rule of thumb: **peak process memory is roughly the total input size plus
~40 MB of fixed overhead**, and it depends on total bytes, not file count.
Decrypt/re-encrypt adds CPU time (about a second per 250 MB here), not memory.
The cgroup number runs higher because it also counts the page cache the run
touched; that cache is reclaimable, so it does not need to fit under a memory
limit. Practical Kubernetes guidance: set `requests.memory` and
`limits.memory` to about **total expected input size + 128 MB**. A run that
exceeds the limit is OOM-killed (SIGKILL) - no terminal event is emitted, and
the workflow engine reports the kill itself.

## Log events

One JSON object per line on stdout; stderr stays empty. Every event carries `ts`,
`level` and `event`; the other fields depend on the event. Lifecycle events respect
`PDFOPS_LOG_LEVEL`; the two terminal events never do. This is the complete
vocabulary: a test checks it against every event the code emits.

| Event | Level | When, and what it carries |
|---|---|---|
| `config_loaded` | info | the parsed configuration: `operation`, `log_level`, `on_exists`, and `password` / `output_password` as presence only (`unset` / `set(env)` / `set(file)`), never values |
| `operation_started` | info | dispatch into merge or extract |
| `input_opened` | info | per input: `input`, `pages`, `encrypted`, `algorithm`, `password_type` (`user` / `owner` / `empty`) |
| `pdf_library_message` | warning | `detail` and `source`: damage the engine repaired (`source: qpdf`), or anything the PDF library or a Python warning routes through logging - those records bypass `PDFOPS_LOG_LEVEL` |
| `password_unused` | warning | a password was supplied but no input needed it |
| `security_downgrade` | warning | encrypted inputs merged into a plaintext output under `never`: `encrypted_inputs` |
| `redaction_degraded` | warning | a supplied secret is shorter than four characters and is not scrubbed from free text |
| `stale_temp_removed` | warning | a prior run's temp debris for this target was removed: `temp_file` |
| `output_skipped` | info | merge under `skip`: the existing output is accepted as completed work, `output_path` |
| `output_overwritten` | info | after the write: `output_path` for merge, `replaced` and `count` for extract |
| `output_encrypted` | info | the merged output was encrypted: `algorithm`, `password_source` (`output` / `input-fallback`) |
| `merge_written` | info | `output_path`, `pages_per_input`, `output_encrypted` |
| `attachments_skipped` | info | extract under `skip`: `skipped` (names), `count` |
| `attachment_extracted` | info | per file: `attachment`, `bytes`, and `original_name` when sanitization changed it (capped at 200 characters) |
| `operation_complete` | info | terminal, exit 0. Always `operation`, `exit_code`, `duration_s`. Merge adds `inputs_merged`, `pages`, `bytes_written`, `output_path`, `output_encrypted`, or under `skip` only `skipped: true` and `output_path`. Extract adds `attachments_extracted`, `bytes_written`, and `attachments_skipped` when any file was skipped |
| `operation_failed` | error | terminal, exit 1-6. Always `error_code`, `exit_code`, `duration_s`. Predictable failures add `error_message` and `context`; an unexpected error (exit 1) adds `exc_type` and `traceback` instead |

### Error codes

`error_code` values, grouped by the exit code they travel with. This is the
complete vocabulary: a test checks the table against the `ErrorCode` enum in
`errors.py`, so a new code cannot ship undocumented.

| Code | Exit | Meaning |
|---|---|---|
| `UNEXPECTED_ERROR` | 1 | an internal error; the event carries `exc_type` and `traceback` |
| `UNKNOWN_VAR` | 2 | a `PDFOPS_*` variable the application does not understand (probable typo) |
| `INAPPLICABLE_VAR` | 2 | a variable that belongs to the other operation |
| `MISSING_VAR` | 2 | a required variable is unset or empty |
| `INVALID_OPERATION` | 2 | `PDFOPS_OPERATION` is not `merge` or `extract` |
| `INVALID_LOG_LEVEL` | 2 | `PDFOPS_LOG_LEVEL` is not one of the accepted levels |
| `INVALID_INPUTS` | 2 | `PDFOPS_INPUTS` has an empty path component |
| `DUPLICATE_INPUTS` | 2 | `PDFOPS_INPUTS` lists the same path more than once |
| `INVALID_FLAG` | 2 | a boolean variable is not `true` or `false` |
| `INVALID_ON_EXISTS` | 2 | `PDFOPS_ON_EXISTS` is not `fail`, `overwrite` or `skip` |
| `INVALID_OUTPUT_ENCRYPTION` | 2 | `PDFOPS_OUTPUT_ENCRYPTION` is not `never`, `inherit` or `always` |
| `CONFLICTING_PASSWORD_SOURCES` | 2 | both the value and the file channel of one password are set |
| `OUTPUT_PASSWORD_WITHOUT_ENCRYPTION` | 2 | an output password is supplied while output encryption is `never` |
| `MISSING_OUTPUT_PASSWORD` | 2 | output encryption is required but no explicit password is available |
| `PASSWORD_FILE_UNREADABLE` | 2 | the password file cannot be read or is not UTF-8 text |
| `EMPTY_PASSWORD` | 2 | the password file is empty |
| `PASSWORD_UNSUPPORTED_CHARACTERS` | 2 | the password contains control characters |
| `INPUT_MISSING` | 3 | an input path does not exist or is not a regular file |
| `INPUT_IS_DIRECTORY` | 3 | an input path is a directory |
| `INPUT_UNREADABLE` | 3 | an input file cannot be opened for reading |
| `NO_ATTACHMENTS` | 3 | the PDF has no attachments and `PDFOPS_FAIL_ON_NO_ATTACHMENTS=true` |
| `NOT_A_PDF` | 4 | an input does not start with the `%PDF-` header |
| `CORRUPT_PDF` | 4 | the PDF engine cannot parse or fully read the file |
| `UNSUPPORTED_PDF_FEATURE` | 4 | the file uses a stream filter this build cannot decode |
| `PASSWORD_REQUIRED` | 5 | the input is encrypted and no password was supplied |
| `WRONG_PASSWORD` | 5 | the supplied password does not open the input |
| `UNSUPPORTED_ENCRYPTION` | 5 | the input uses an encryption scheme this build cannot process |
| `OUTPUT_DIR_MISSING` | 6 | the output directory does not exist |
| `OUTPUT_IS_DIRECTORY` | 6 | an output path is a directory |
| `OUTPUT_EXISTS` | 6 | an output exists and `PDFOPS_ON_EXISTS` is `fail` |
| `OUTPUT_NOT_WRITABLE` | 6 | the output location is not writable |
| `DISK_FULL` | 6 | no space left on device while writing |
