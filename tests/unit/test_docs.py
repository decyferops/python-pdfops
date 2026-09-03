"""Documentation that doubles as a contract stays in step with the code."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "pdf_ops"

# Every way the package names a log event: a logger call, the terminal emitter,
# and the third-party filter that rewrites foreign records.
_EVENT_PATTERNS = (
    re.compile(r'logger\.(?:debug|info|warning|error)\(\s*"([a-z_]+)"'),
    re.compile(r'emit_terminal\([^)]*?"([a-z_]+)"', re.DOTALL),
    re.compile(r'record\.msg = "([a-z_]+)"'),
)


def test_every_module_is_in_the_architecture_doc() -> None:
    doc = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    modules = sorted(p.name for p in SRC.glob("*.py") if p.name != "__init__.py")
    missing = [name for name in modules if name not in doc]
    assert modules and not missing, missing


def test_every_log_event_is_documented() -> None:
    emitted: set[str] = set()
    for path in SRC.glob("*.py"):
        text = path.read_text()
        for pattern in _EVENT_PATTERNS:
            emitted.update(pattern.findall(text))
    guide = (ROOT / "docs" / "OPERATIONS.md").read_text()
    missing = sorted(name for name in emitted if f"`{name}`" not in guide)
    assert emitted and not missing, missing
