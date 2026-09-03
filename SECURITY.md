# Security policy

pdf-ops parses untrusted PDFs and writes their attachment names to a mounted
filesystem, so input handling is security-relevant by design. The posture -
attachment-name sanitization, the layered no-leak guarantee for passwords,
atomic outputs, a hardened read-only container - is described in
`docs/DESIGN.md`.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
on this repository (Security tab, "Report a vulnerability"); if that is not
enabled, contact the maintainer directly. Include a way to reproduce: the PDF
or how to build one, the `PDFOPS_*` configuration, and the log lines.

## In scope

- An attachment name that writes outside `PDFOPS_OUTPUT_DIR`.
- Password material appearing in any output: stdout, stderr, files, tracebacks.
- A partial or mixed output surviving a failed or killed run.
- A deterministic bad input classified as retryable (exit 1).
- A hostile PDF structure that escapes the error taxonomy (exit 1) instead of
  classifying as a data problem.
