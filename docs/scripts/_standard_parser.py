"""Single-source-of-truth parser + shared regex constants for the decision-tracking framework.

This module is the canonical location for:
  1. Controlled vocabularies (AREAS, STATUS_ICONS, RISKS, REVERSIBILITIES, PHASES) parsed
     from the `## Controlled vocabularies (machine-parseable)` section of
     DECISION_TRACKING_STANDARD.md.
  2. Shared parsing regexes used across all framework scripts (add_decision.py,
     validate_decisions.py, update_index.py, supersede_decision.py, amend_decision.py).

Importing this module triggers the standard file to be read once. Parse failures print a
clear error pointing at the standard file and exit non-zero. Edit the standard's
controlled-vocabularies section to change vocabularies - every script picks up the change.
"""

import re
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Shared regex constants - imported by every framework script.
# Kept here so that all parsing patterns have one source of truth.
# -----------------------------------------------------------------------------

# Strict 8-column master-register row:
# | [`D-###`](#D-###) | <status icon + text> | <area> | <title> | <YYYY-MM-DD> | <summary> | <discussion cell> | <ADR cell> |
MASTER_ROW_RE = re.compile(
    r"^\|\s*\[`D-(\d{3})`\]\(#D-\d{3}\)\s*\|\s*([^|]+?)\s*\|"
    r"\s*([a-z-]+)\s*\|\s*(.+?)\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|"
    r"\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$"
)

# Anchor page heading: ### D-001
ANCHOR_HEADING_RE = re.compile(r"^###\s+D-(\d{3})\s*$")

# Index by area row: | <area-or-Total> | <count> | <ID list> |
INDEX_ROW_RE = re.compile(
    r"^\|\s*(?:\*\*)?([a-z-]+|Total)(?:\*\*)?\s*\|\s*(?:\*\*)?(\d+)(?:\*\*)?\s*\|"
    r"\s*(.*?)\s*\|\s*$"
)

# Generic D-### / ADR-### references inside prose or anchor fields.
ID_RE = re.compile(r"\bD-(\d{3})\b")
ADR_REF_RE = re.compile(r"\bADR-(\d{3})\b")
ADR_HEADING_RE = re.compile(r"^##\s+ADR-(\d{3})\s*:")

# Markdown link: [text](path)
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

# ISO date prefix (start of "Decided on" field).
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

# Anchor field line: "- **Field:** value"
ANCHOR_FIELD_RE = re.compile(r"^-\s*\*\*([^:]+):\*\*\s*(.*)$")

# -----------------------------------------------------------------------------
# Vocabulary parsing
# -----------------------------------------------------------------------------


# Standard path resolution (v0.11.27): prefer --standard-path CLI arg if
# provided, fall back to relative-to-script default. The default resolves
# correctly when this script lives at docs/scripts/ in a scaffolded project
# (parent.parent -> docs/). When invoked from skill-side
# (~/.claude/skills/init-decisions/templates/), the default fails because
# the standard isn't co-located - pass --standard-path explicitly. The arg
# unblocks verify-framework Ability's documented CLI invocation against
# legacy projects whose docs/scripts/ doesn't have a copy of this script.
def _resolve_standard_path() -> Path:
    import sys

    if "--standard-path" in sys.argv:
        idx = sys.argv.index("--standard-path")
        if idx + 1 < len(sys.argv):
            path = Path(sys.argv[idx + 1]).resolve()
            # Pop the flag + value so consuming scripts' argparse doesn't reject them.
            del sys.argv[idx : idx + 2]
            return path
    return Path(__file__).resolve().parent.parent / "DECISION_TRACKING_STANDARD.md"


STANDARD_PATH = _resolve_standard_path()

# Required H3 subsections under `## Controlled vocabularies (machine-parseable)`.
_REQUIRED_SECTIONS: tuple[str, ...] = (
    "Areas",
    "Status icons",
    "Risk values",
    "Reversibility values",
    "Implementation phases",
)

_VOCAB_HEADER_RE = re.compile(r"^##\s+Controlled vocabularies\s+\(machine-parseable\)\s*$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")
_NEXT_H2_RE = re.compile(r"^##\s+")
# A backtick-quoted item on its own bullet line: "- `value`" or "- `value` description"
_BACKTICK_ITEM_RE = re.compile(r"^-\s*`([^`]+)`")


def _parse_vocab_sections(text: str) -> dict[str, list[str]]:
    """Walk lines, locate the vocab H2, collect H3 subsections with their backtick items."""
    result: dict[str, list[str]] = {}
    in_vocab = False
    current: str | None = None

    for line in text.splitlines():
        if _VOCAB_HEADER_RE.match(line):
            in_vocab = True
            continue
        if in_vocab and _NEXT_H2_RE.match(line):
            break
        if not in_vocab:
            continue
        h3 = _H3_RE.match(line)
        if h3:
            name = h3.group(1).strip()
            current = name
            result.setdefault(name, [])
            continue
        if current is None:
            continue
        item = _BACKTICK_ITEM_RE.match(line)
        if item:
            result[current].append(item.group(1).strip())

    return result


def _abort(msg: str) -> None:
    print(f"ERROR (_standard_parser): {msg}", file=sys.stderr)
    print(f"  standard path: {STANDARD_PATH}", file=sys.stderr)
    sys.exit(2)


def _load() -> dict[str, frozenset[str]]:
    if not STANDARD_PATH.exists():
        _abort(
            "DECISION_TRACKING_STANDARD.md not found - required for vocabulary parsing. "
            "Run /init-decisions to scaffold it."
        )
    text = STANDARD_PATH.read_text(encoding="utf-8")
    sections = _parse_vocab_sections(text)
    missing = [s for s in _REQUIRED_SECTIONS if s not in sections]
    if missing:
        _abort(
            f"missing required H3 subsection(s) under "
            f"'## Controlled vocabularies (machine-parseable)': {missing}"
        )
    empty = [s for s in _REQUIRED_SECTIONS if not sections[s]]
    if empty:
        _abort(
            f"H3 subsection(s) {empty} under '## Controlled vocabularies (machine-parseable)' "
            f"are present but contain no backtick-quoted items"
        )
    return {
        "AREAS": frozenset(sections["Areas"]),
        "STATUS_ICONS": frozenset(sections["Status icons"]),
        "RISKS": frozenset(sections["Risk values"]),
        "REVERSIBILITIES": frozenset(sections["Reversibility values"]),
        "PHASES": frozenset(sections["Implementation phases"]),
    }


_vocabs = _load()
AREAS: frozenset[str] = _vocabs["AREAS"]
STATUS_ICONS: frozenset[str] = _vocabs["STATUS_ICONS"]
RISKS: frozenset[str] = _vocabs["RISKS"]
REVERSIBILITIES: frozenset[str] = _vocabs["REVERSIBILITIES"]
PHASES: frozenset[str] = _vocabs["PHASES"]


if __name__ == "__main__":
    for name, val in _vocabs.items():
        print(f"{name} = {sorted(val)}")
