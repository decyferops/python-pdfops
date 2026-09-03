#!/usr/bin/env python3
"""Validate the decision tracking register in docs/DECISIONS.md.

Checks performed (all exit-code failing):

  1. ID sequence has no gaps (D-001..D-NNN contiguous).
  2. Every master-register row has a matching anchor page and vice versa.
  3. Every anchor page has the required fields
     (Title, Status, Area, Decided on, Summary, Where).
  4. Every `area` value belongs to the standard's area taxonomy.
  5. Recomputed `Index by area` counts match what is written.
  6. `related` IDs (anchor pages + master table) resolve to existing D-###.
  7. `discussion` / `Where` links resolve to real files inside docs/.
  8. Total count row matches the actual count.
  9. Every 🟢 decision has a non-empty Summary on its anchor page
     (not blank, not a TBD placeholder).
 10. Every `ADR-###` link in the master register's "ADR" column resolves
     to an `## ADR-###: ...` heading in the same document.
 11. Every decision whose Status contains "assumed" (decide-under-assumption
     pattern) has a `Revisit trigger:` field AND a corresponding row in
     `planning/ASSUMPTIONS.md` referencing the decision's `D-###`.

Controlled vocabularies (areas, status icons, risks, reversibilities, phases)
are imported from `_standard_parser`, which reads them from
`DECISION_TRACKING_STANDARD.md`. The standard is the single source of truth.

Usage:
    python docs/scripts/validate_decisions.py [path/to/DECISIONS.md]

Exit code 0 on clean run, 1 on any violation. Prints one line per issue.
"""

import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from _standard_parser import (
    ADR_HEADING_RE,
    ADR_REF_RE,
    ANCHOR_FIELD_RE,
    ANCHOR_HEADING_RE,
    AREAS,
    ID_RE,
    INDEX_ROW_RE,
    ISO_DATE_RE,
    LINK_RE,
    MASTER_ROW_RE,
    PHASES,
    REVERSIBILITIES,
    RISKS,
    STATUS_ICONS,
)

# Structural constants (not vocabularies - these are part of the framework's
# document shape and don't change per-project).
REQUIRED_ANCHOR_FIELDS: tuple[str, ...] = (
    "Title",
    "Status",
    "Area",
    "Decided on",
    "Summary",
    "Where",
)

GREEN_STATUS_ICON = "🟢"
SUMMARY_PLACEHOLDERS: frozenset[str] = frozenset({"tbd", "todo", "tba", "-", " - ", "n/a"})


@dataclass
class Decision:
    id: str
    status: str = ""
    area: str = ""
    title: str = ""
    decided: str = ""
    summary: str = ""
    discussion_raw: str = ""
    adr_raw: str = ""
    anchor_fields: dict[str, str] = field(default_factory=dict)
    anchor_lineno: int = 0
    master_lineno: int = 0

    @property
    def num(self) -> int:
        return int(self.id.split("-")[1])


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self) -> bool:
        return not self.errors


def parse_master_register(
    lines: list[str], report: Report
) -> tuple[dict[str, Decision], int | None]:
    """Return (decisions, master_total_claimed)."""
    decisions: dict[str, Decision] = {}
    total_claimed: int | None = None
    in_master = False
    master_header_seen = False
    in_fence = False

    for i, line in enumerate(lines, start=1):
        stripped = line.rstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped == "## Master Register":
            in_master = True
            continue
        if in_master and stripped.startswith(("## ", "### ")) and stripped != "## Master Register":
            in_master = False
        if not in_master:
            continue
        if stripped.startswith("| ID |"):
            master_header_seen = True
            continue
        m = MASTER_ROW_RE.match(stripped)
        if m:
            num = m.group(1)
            did = f"D-{num}"
            if did in decisions:
                report.err(f"{did}: duplicate master register row at line {i}")
                continue
            decisions[did] = Decision(
                id=did,
                status=m.group(2).strip(),
                area=m.group(3).strip(),
                title=m.group(4).strip(),
                decided=m.group(5).strip(),
                summary=m.group(6).strip(),
                discussion_raw=m.group(7).strip(),
                adr_raw=m.group(8).strip(),
                master_lineno=i,
            )
            continue
        tm = re.match(r"\*\*Counts:\*\*\s+(\d+)\s+total", stripped)
        if tm:
            total_claimed = int(tm.group(1))

    if not master_header_seen:
        report.err("Master Register table header not found")
    return decisions, total_claimed


def parse_anchor_pages(lines: list[str], decisions: dict[str, Decision], report: Report) -> None:
    """Populate anchor_fields on each Decision by reading its anchor page."""
    i = 0
    n = len(lines)
    in_anchors = False
    in_fence = False
    while i < n:
        line = lines[i].rstrip("\n")
        if line.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        if line == "## Decision Anchor Pages":
            in_anchors = True
            i += 1
            continue
        if in_anchors and line.startswith("## ") and line != "## Decision Anchor Pages":
            in_anchors = False
        if not in_anchors:
            i += 1
            continue

        m = ANCHOR_HEADING_RE.match(line)
        if not m:
            i += 1
            continue

        did = f"D-{m.group(1)}"
        start_line = i + 1
        fields: dict[str, str] = {}
        j = i + 1
        while j < n:
            sub = lines[j].rstrip("\n")
            if ANCHOR_HEADING_RE.match(sub) or sub.startswith(("## ", "---")):
                break
            fm = ANCHOR_FIELD_RE.match(sub)
            if fm:
                key = fm.group(1).strip()
                val = fm.group(2).strip()
                fields[key] = val
            j += 1

        if did not in decisions:
            report.err(
                f"{did}: anchor page at line {start_line} has no matching master register row"
            )
        else:
            decisions[did].anchor_fields = fields
            decisions[did].anchor_lineno = start_line
        i = j


def check_sequence(decisions: dict[str, Decision], report: Report) -> None:
    nums = sorted(d.num for d in decisions.values())
    if not nums:
        report.err("no decisions found")
        return
    if nums[0] != 1:
        report.err(f"ID sequence starts at D-{nums[0]:03d}, expected D-001")
    for prev, curr in pairwise(nums):
        if curr != prev + 1:
            report.err(
                f"ID sequence gap: D-{prev:03d} -> D-{curr:03d} "
                f"(missing D-{prev + 1:03d}..D-{curr - 1:03d})"
            )


def check_anchors_complete(decisions: dict[str, Decision], report: Report) -> None:
    for did, d in sorted(decisions.items()):
        if not d.anchor_fields:
            report.err(f"{did}: master row at line {d.master_lineno} has no anchor page")
            continue
        missing = [f for f in REQUIRED_ANCHOR_FIELDS if f not in d.anchor_fields]
        if missing:
            report.err(
                f"{did}: anchor page at line {d.anchor_lineno} missing required "
                f"field(s): {', '.join(missing)}"
            )


def check_areas(decisions: dict[str, Decision], report: Report) -> None:
    for did, d in sorted(decisions.items()):
        if d.area not in AREAS:
            report.err(
                f"{did}: area '{d.area}' not in standard's taxonomy ({', '.join(sorted(AREAS))})"
            )
        anchor_area = d.anchor_fields.get("Area", "").strip()
        if anchor_area and anchor_area != d.area:
            report.err(f"{did}: area mismatch - master='{d.area}' anchor='{anchor_area}'")


def check_vocabularies(decisions: dict[str, Decision], report: Report) -> None:
    """Enforce controlled vocabularies for optional fields + Status icon + date format."""
    for did, d in sorted(decisions.items()):
        f = d.anchor_fields

        risk = f.get("Risk", "").strip().lower()
        if risk and risk not in RISKS:
            report.err(
                f"{did}: Risk value '{f.get('Risk')}' not in "
                f"{{{', '.join(sorted(RISKS))}}} (anchor line {d.anchor_lineno})"
            )

        rev = f.get("Reversibility", "").strip().lower()
        if rev and rev not in REVERSIBILITIES:
            report.err(
                f"{did}: Reversibility value '{f.get('Reversibility')}' not in "
                f"{{{', '.join(sorted(REVERSIBILITIES))}}} (anchor line {d.anchor_lineno})"
            )

        phase = f.get("Implementation phase", "").strip()
        if phase and phase not in PHASES:
            report.err(
                f"{did}: Implementation phase value '{phase}' not in "
                f"{{{', '.join(sorted(PHASES))}}} (anchor line {d.anchor_lineno})"
            )

        status = f.get("Status", "").strip()
        if status:
            icon = status.split(" ", 1)[0] if " " in status else status
            if icon not in STATUS_ICONS:
                report.err(
                    f"{did}: Status icon '{icon}' not in "
                    f"{{{' '.join(sorted(STATUS_ICONS))}}} (anchor line {d.anchor_lineno})"
                )

        decided = f.get("Decided on", "").strip()
        if decided and not ISO_DATE_RE.match(decided):
            report.err(
                f"{did}: Decided on must start with an ISO date (YYYY-MM-DD), "
                f"got '{decided[:30]}' (anchor line {d.anchor_lineno})"
            )


def check_index_counts(lines: list[str], decisions: dict[str, Decision], report: Report) -> None:
    """Verify the 'Index by area' table agrees with the master register."""
    actual_counts: Counter[str] = Counter(d.area for d in decisions.values())
    actual_ids: dict[str, list[str]] = defaultdict(list)
    for d in sorted(decisions.values(), key=lambda x: x.num):
        actual_ids[d.area].append(d.id)

    in_index = False
    found_total = False
    in_fence = False
    claimed_areas: set[str] = set()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n")
        if line.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        if line.startswith("### Index by area"):
            in_index = True
            i += 1
            continue
        if in_index and line.startswith("### "):
            break
        if not in_index:
            i += 1
            continue
        m = INDEX_ROW_RE.match(line)
        if m:
            area = m.group(1)
            count = int(m.group(2))
            ids_cell = m.group(3).strip()
            if area == "Total":
                found_total = True
                if count != len(decisions):
                    report.err(
                        f"Index by area: Total count {count} != actual {len(decisions)} "
                        f"(line {i + 1})"
                    )
            else:
                claimed_areas.add(area)
                if area not in AREAS:
                    report.err(f"Index by area: unknown area '{area}' at line {i + 1}")
                    i += 1
                    continue
                if count != actual_counts[area]:
                    report.err(
                        f"Index by area: area '{area}' count {count} != actual "
                        f"{actual_counts[area]} at line {i + 1}"
                    )
                listed = sorted(set(ID_RE.findall(ids_cell)))
                actual_set = sorted({i.split("-")[1] for i in actual_ids[area]})
                if listed != actual_set:
                    missing = sorted(set(actual_set) - set(listed))
                    extra = sorted(set(listed) - set(actual_set))
                    parts = []
                    if missing:
                        parts.append(f"missing {', '.join('D-' + m for m in missing)}")
                    if extra:
                        parts.append(f"extra {', '.join('D-' + e for e in extra)}")
                    report.err(
                        f"Index by area: area '{area}' ID list drift at line {i + 1} - "
                        + "; ".join(parts)
                    )
        i += 1

    if not found_total:
        report.err("Index by area: Total row not found")

    for area in AREAS:
        if actual_counts[area] and area not in claimed_areas:
            report.err(
                f"Index by area: area '{area}' has {actual_counts[area]} decisions but no row"
            )


def check_related_ids(decisions: dict[str, Decision], report: Report) -> None:
    """Every D-### referenced in anchor fields must resolve."""
    known = set(decisions.keys())
    for did, d in sorted(decisions.items()):
        for field_name in ("Related", "Supersedes", "Superseded by", "Amends", "Amended by"):
            val = d.anchor_fields.get(field_name, "")
            for ref in ID_RE.findall(val):
                target = f"D-{ref}"
                if target not in known:
                    report.err(
                        f"{did}: {field_name} -> {target} does not resolve "
                        f"(anchor line {d.anchor_lineno})"
                    )


def check_doc_links(decisions: dict[str, Decision], docs_root: Path, report: Report) -> None:
    """Every `discussion` / `Where` link target (relative path) must exist in docs/."""
    for did, d in sorted(decisions.items()):
        candidates: list[tuple[str, str]] = []
        if d.discussion_raw:
            candidates.append(("discussion", d.discussion_raw))
        where_val = d.anchor_fields.get("Where", "")
        if where_val:
            candidates.append(("Where", where_val))

        for label, raw in candidates:
            for _, target in LINK_RE.findall(raw):
                path_part = target.split("#", 1)[0].split(" ", 1)[0]
                if not path_part:
                    continue
                if path_part.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (docs_root / path_part).resolve()
                if not resolved.exists():
                    report.err(f"{did}: {label} link '{path_part}' -> {resolved} does not exist")


def check_green_summaries(decisions: dict[str, Decision], report: Report) -> None:
    """Every 🟢 decision must carry a non-empty Summary on its anchor page."""
    for did, d in sorted(decisions.items()):
        status = d.anchor_fields.get("Status", d.status).strip()
        if not status.startswith(GREEN_STATUS_ICON):
            continue
        summary = d.anchor_fields.get("Summary", "").strip()
        if not summary:
            report.err(
                f"{did}: 🟢 decision has empty Summary on anchor page "
                f"(anchor line {d.anchor_lineno})"
            )
            continue
        if summary.lower() in SUMMARY_PLACEHOLDERS:
            report.err(
                f"{did}: 🟢 decision Summary is a placeholder "
                f"('{summary}') (anchor line {d.anchor_lineno})"
            )


def check_adr_references(lines: list[str], decisions: dict[str, Decision], report: Report) -> None:
    """Every ``ADR-###`` link in the master register's "ADR" column must
    resolve to an ``## ADR-###:`` heading in the same document."""
    existing_adrs: set[str] = set()
    for line in lines:
        m = ADR_HEADING_RE.match(line.rstrip())
        if m:
            existing_adrs.add(m.group(1))

    for did, d in sorted(decisions.items()):
        for num in ADR_REF_RE.findall(d.adr_raw):
            if num not in existing_adrs:
                report.err(
                    f"{did}: master-register ADR reference 'ADR-{num}' "
                    f"has no '## ADR-{num}:' heading "
                    f"(master line {d.master_lineno})"
                )
        full_adr = d.anchor_fields.get("Full ADR", "")
        for num in ADR_REF_RE.findall(full_adr):
            if num not in existing_adrs:
                report.err(
                    f"{did}: anchor-page 'Full ADR' reference 'ADR-{num}' "
                    f"has no '## ADR-{num}:' heading "
                    f"(anchor line {d.anchor_lineno})"
                )


def check_decide_under_assumption_cross_links(
    decisions: dict[str, Decision], docs_root: Path, report: Report
) -> None:
    """Decide-under-assumption pattern: decisions whose Status contains 'assumed'
    must have a `Revisit trigger:` field AND a corresponding row in
    `planning/ASSUMPTIONS.md` referencing the decision's `D-###`.

    See the 'Workflow - decide under assumption' section of
    DECISION_TRACKING_STANDARD.md for the rationale.
    """
    assumed = [
        d
        for d in decisions.values()
        if "assumed" in d.anchor_fields.get("Status", d.status).lower()
    ]
    if not assumed:
        return

    assumptions_path = docs_root / "planning" / "ASSUMPTIONS.md"
    assumptions_text: str | None = None
    if assumptions_path.exists():
        assumptions_text = assumptions_path.read_text(encoding="utf-8")

    for d in sorted(assumed, key=lambda x: x.num):
        revisit = d.anchor_fields.get("Revisit trigger", "").strip()
        if not revisit:
            report.err(
                f"{d.id}: 🟢-with-'assumed' Status missing required `Revisit trigger:` "
                f"field on anchor page (line {d.anchor_lineno})"
            )

        if assumptions_text is None:
            report.err(
                f"{d.id}: 🟢-with-'assumed' Status but {assumptions_path} not found - "
                f"decide-under-assumption requires a matching ASSUMPTIONS.md row"
            )
            continue
        if d.id not in assumptions_text:
            report.err(
                f"{d.id}: 🟢-with-'assumed' Status but ASSUMPTIONS.md has no reference "
                f"to {d.id} - expected a cross-link row in section 1"
            )


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path = Path(argv[1])
    else:
        path = Path(__file__).resolve().parent.parent / "DECISIONS.md"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2

    docs_root = path.parent
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    report = Report()
    decisions, total_claimed = parse_master_register(lines, report)
    parse_anchor_pages(lines, decisions, report)
    check_sequence(decisions, report)
    check_anchors_complete(decisions, report)
    check_areas(decisions, report)
    check_vocabularies(decisions, report)
    check_index_counts(lines, decisions, report)
    check_related_ids(decisions, report)
    check_doc_links(decisions, docs_root, report)
    check_green_summaries(decisions, report)
    check_adr_references(lines, decisions, report)
    check_decide_under_assumption_cross_links(decisions, docs_root, report)

    if total_claimed is not None and total_claimed != len(decisions):
        report.err(f"Counts line claims {total_claimed} total decisions, actual = {len(decisions)}")

    rel_path = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path
    print(f"Parsed {len(decisions)} decisions from {rel_path}")
    if report.warnings:
        print(f"\n{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  WARN  {w}")
    if report.errors:
        print(f"\n{len(report.errors)} error(s):")
        for e in report.errors:
            print(f"  FAIL  {e}")
        return 1
    print("OK - all 11 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
