# Architecture in diagrams

Eight views of the same system, drawn so they render on GitHub, in pull requests
and in most editors. Each says why the shape is what it is and links into the
interactive deep-dive at [`diagrams/index.html`](diagrams/index.html), where nodes
link to the source lines they describe. Prose lives in [`DESIGN.md`](DESIGN.md), the
operator contract in [`OPERATIONS.md`](OPERATIONS.md), and every choice in the
decision register [`DECISIONS.md`](DECISIONS.md).

A test (`tests/unit/test_docs.py`) checks that every module under `src/pdf_ops` is
named on this page, so the module view cannot drift from the tree.

## 1. Where it runs

One container run is one workflow step. Everything the process needs arrives as
environment variables and mounted volumes; everything it reports leaves as JSON
lines on stdout and an exit code. The password file is read late and only when the
run actually proceeds, which is why a retry after a lost-but-successful pod works
even when the secret mount is already gone.

```mermaid
flowchart LR
    engine["Workflow engine<br/>Argo step with retryStrategy"]
    subgraph pod["Pod: one container run"]
        env["PDFOPS_* environment"]
        proc["python -m pdf_ops<br/>UID 10001, read-only rootfs"]
        data[("/data volume<br/>inputs read, outputs written atomically")]
        secret[("/secrets volume<br/>password file, read-only")]
    end
    engine -- "env, volumes, memory limit" --> env
    env --> proc
    data <--> proc
    secret -. "read late, only if the run proceeds" .-> proc
    proc -- "JSON lines on stdout" --> engine
    proc -- "exit code 0-6" --> engine
```

Deep-dive: the deployment posture this is tested under is
[`../deploy/argo-example.yaml`](../deploy/argo-example.yaml).

## 2. Modules and the direction of dependency

Every arrow is a real `import`; nothing points upward. `errors.py` and
`logging_setup.py` import nothing from the package, so anything may use them.
`engine_pikepdf.py` is the only module that imports pikepdf, reached through
`engine.py`'s `get_engine()` with a lazy import, so the seam knows its
implementation while the rest of the package never does.

```mermaid
flowchart TB
    subgraph entry["Entry and orchestration"]
        n_entry["__main__.py<br/>process boundary: os.environ in, exit code out"]
        n_main["main.py<br/>run(env): the one error boundary"]
    end
    subgraph ops["Operations"]
        n_merge["merge.py<br/>validate everything, then write once"]
        n_extract["extract.py<br/>untrusted names in, atomic files out"]
    end
    subgraph seam["Engine seam"]
        n_engine["engine.py<br/>PdfEngine protocol, OpenedInput"]
        n_pike["engine_pikepdf.py<br/>the only module importing pikepdf"]
    end
    subgraph found["Foundations"]
        n_config["config.py<br/>pure parse of the env contract"]
        n_inputs["inputs.py<br/>up-front input validation"]
        n_output["output.py<br/>atomic writes, existing-output policy"]
        n_secrets["secrets.py<br/>Secret wrapper, refs, late resolution"]
        n_logging["logging_setup.py<br/>JSON lines, scrubbing, terminal events"]
        n_errors["errors.py<br/>ExitCode, ErrorCode, the error classes"]
    end
    n_entry --> n_main
    n_entry --> n_config
    n_main --> n_config
    n_main --> n_merge
    n_main --> n_extract
    n_main --> n_secrets
    n_main --> n_logging
    n_main --> n_errors
    n_merge --> n_config
    n_merge --> n_engine
    n_merge --> n_inputs
    n_merge --> n_output
    n_merge --> n_secrets
    n_merge --> n_errors
    n_extract --> n_config
    n_extract --> n_engine
    n_extract --> n_inputs
    n_extract --> n_output
    n_extract --> n_secrets
    n_extract --> n_errors
    n_engine -. "lazy, inside get_engine()" .-> n_pike
    n_engine --> n_secrets
    n_pike --> n_engine
    n_pike --> n_secrets
    n_pike --> n_errors
    n_config --> n_secrets
    n_config --> n_errors
    n_output --> n_config
    n_output --> n_errors
    n_inputs --> n_errors
    n_secrets --> n_logging
    n_secrets --> n_errors
```

Deep-dive: [`diagrams/index.html#architecture`](diagrams/index.html#architecture).

## 3. One run, end to end

`run(env)` is the only place that catches exceptions and the only place that emits a
terminal event. Configuration is parsed completely before any file is touched;
secrets resolve only after the existing-output policy has decided the run proceeds;
output goes to a temp file in the destination directory and is renamed in one step.

```mermaid
sequenceDiagram
    autonumber
    participant E as __main__
    participant R as main.run
    participant C as config
    participant O as merge / extract
    participant F as inputs / output
    participant S as secrets
    participant P as engine_pikepdf
    participant L as logging_setup
    E->>R: run(env)
    R->>L: setup_logging()
    R->>C: parse_config(env)
    C-->>R: MergeConfig or ExtractConfig, else ConfigError
    R->>L: config_loaded (passwords as presence only)
    R->>O: dispatch by config type
    O->>F: check_output_path / validate_inputs
    O->>S: resolve_and_register (late, only if the run proceeds)
    O->>P: open_input(path, password)
    P-->>O: OpenedInput: pages, encryption facts, repair warnings
    O->>F: atomic_output(path): temp file beside the target
    O->>P: merge_to / list_attachments
    F-->>O: fsync and rename, or cleanup on failure
    O-->>R: result fields
    R->>L: emit_terminal(operation_complete or operation_failed)
    R-->>E: exit code
```

## 4. Failures: classes, codes, exit codes

The exit code is the external API and stays small. Each error class maps to exactly
one code; the finer `error_code` travels in the terminal event. Anything that is not
a `PdfOpsError` is a bug and exits 1, the only class an engine should retry. The
complete code table is in [`OPERATIONS.md#error-codes`](OPERATIONS.md#error-codes).

```mermaid
flowchart LR
    any["A failure inside run(env)"] --> pred{"PdfOpsError?"}
    pred -- "no" --> c1["exit 1 UNEXPECTED<br/>UNEXPECTED_ERROR, exc_type, traceback"]
    pred -- "yes" --> cls{"which class?"}
    cls --> c2["ConfigError, exit 2<br/>UNKNOWN_VAR, MISSING_VAR, INVALID_*, DUPLICATE_INPUTS,<br/>CONFLICTING_PASSWORD_SOURCES, MISSING_OUTPUT_PASSWORD,<br/>PASSWORD_FILE_UNREADABLE, EMPTY_PASSWORD, ..."]
    cls --> c3["InputError, exit 3<br/>INPUT_MISSING, INPUT_IS_DIRECTORY,<br/>INPUT_UNREADABLE, NO_ATTACHMENTS"]
    cls --> c4["InvalidPdfError, exit 4<br/>NOT_A_PDF, CORRUPT_PDF, UNSUPPORTED_PDF_FEATURE"]
    cls --> c5["PasswordError, exit 5<br/>PASSWORD_REQUIRED, WRONG_PASSWORD, UNSUPPORTED_ENCRYPTION"]
    cls --> c6["OutputError, exit 6<br/>OUTPUT_DIR_MISSING, OUTPUT_IS_DIRECTORY, OUTPUT_EXISTS,<br/>OUTPUT_NOT_WRITABLE, DISK_FULL"]
```

## 5. Extract: the boundary an attachment name crosses

An attachment name is attacker-controlled text that ends up as a filename on a
mounted volume. Every name passes through a pure, table-tested sanitizer, then a
casefolded collision check (the volume may be case-insensitive), then the
existing-output policy, then a containment re-check that backs the sanitizer up,
and only then an atomic write.

```mermaid
flowchart LR
    pdf["Untrusted PDF<br/>/Names/EmbeddedFiles name tree"] --> raw["Raw name<br/>separators, traversal, control characters,<br/>any length, duplicates, or not even a string"]
    raw --> san["sanitize_attachment_name<br/>basename, strip C0/C1, cap at 200 bytes,<br/>deterministic attachment_n fallback"]
    san --> dedupe["_dedupe<br/>casefolded collisions get -1, -2 suffixes"]
    dedupe --> policy{"target exists?"}
    policy -- "fail (default)" --> refuse["OUTPUT_EXISTS, exit 6<br/>nothing written"]
    policy -- "skip" --> keep["completed prior work<br/>left as is"]
    policy -- "absent, or overwrite" --> check["containment re-check<br/>parent of target is PDFOPS_OUTPUT_DIR"]
    check --> write["atomic_output<br/>temp file in the output dir, then rename"]
    write --> fs[("PDFOPS_OUTPUT_DIR")]
```

Deep-dive: [`diagrams/index.html#extract`](diagrams/index.html#extract).

## 6. Retries: the existing-output policy as a state machine

Workflow engines retry at least once, and a pod can vanish after its work
succeeded. `PDFOPS_ON_EXISTS` decides what the next attempt does with an output
that already exists; stale temp debris from a killed write is removed before the
first write of the next attempt.

```mermaid
stateDiagram-v2
    state "Config parsed" as Parsed
    state "Output exists?" as Exists
    state "Temp write" as Writing
    state "Refused: OUTPUT_EXISTS, exit 6" as Refused
    state "Skipped: exit 0, skipped true" as Skipped
    state "Complete: exit 0" as Complete
    state "Killed mid-write" as Killed
    [*] --> Invoked
    Invoked --> Parsed : parse_config
    Invoked --> [*] : ConfigError, exit 2
    Parsed --> Exists : check_output_path
    Exists --> Writing : absent
    Exists --> Writing : present and overwrite
    Exists --> Refused : present and fail (default)
    Exists --> Skipped : present and skip
    Writing --> Complete : fsync, rename, terminal event
    Writing --> Killed : pod lost
    Killed --> Parsed : retry, stale temp removed before the first write
    Refused --> [*]
    Skipped --> [*]
    Complete --> [*]
    note right of Skipped
        merge: whole-run no-op, inputs and password file never read
        extract: per file, only the missing attachments are written
    end note
```

Deep-dive: [`diagrams/index.html#lifecycle`](diagrams/index.html#lifecycle).

## 7. Tests and the cross-library oracle

pypdf is a dev-only dependency with one job: it builds every fixture and re-reads
every output, so each test is a check between two independent PDF libraries.
Fixtures are generated, never checked in. Three tiers run at three costs.

```mermaid
flowchart LR
    subgraph oracle["pypdf: the test oracle"]
        build["conftest factories build fixtures<br/>plain, encrypted, damaged, dangling refs,<br/>hostile name trees, raw attachments"]
        verify["pypdf re-reads the outputs<br/>page counts, encryption, attachment bytes"]
    end
    subgraph sut["pdf_ops with the pikepdf engine"]
        unit["unit: pure functions<br/>config, sanitizer, errors, secrets, logging, docs sync"]
        integ["integration: run(env) in-process<br/>invariants on every run: empty stderr,<br/>JSON lines only, exactly one terminal event, last"]
        cont["container: docker build and run<br/>golden merge and extract, mounted secret,<br/>hardened posture, no package installer"]
    end
    build --> integ
    build --> cont
    integ --> verify
    cont --> verify
```

## 8. From commit to image

One toolchain, one version of each tool, pinned in `uv.lock`: the hooks, CI and a
developer's shell all run ruff and pyright through uv. The image is built from the
same lockfile and ships nothing that could change it.

```mermaid
flowchart LR
    commit["commit"] --> hooks["pre-commit through uv<br/>hygiene, ruff check and format, pyright"]
    hooks --> push["push, pull request"]
    push --> quality["CI quality job<br/>uv sync --locked, ruff, format, pyright,<br/>pytest, decision-register validator"]
    push --> docker["CI docker job<br/>build the image, container contract tests"]
    docker --> image["image: builder stage uv sync --locked, then runtime<br/>digest-pinned base, no pip or ensurepip, UID 10001"]
    bot["Dependabot<br/>uv.lock, actions, base image"] -.-> push
```
