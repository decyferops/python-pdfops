"""The retry contract through run(env): PDFOPS_ON_EXISTS across both
operations, stale-temp cleanup, and run-twice simulations.

Workflow engines are at-least-once: every scenario here is some flavor of
"the step ran before - what does running it again do?"
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from pypdf import PdfReader

import pdf_ops.merge
from pdf_ops.errors import ErrorCode, InvalidPdfError
from tests.helpers import RunApp
from tests.integration.test_extract import extract_env
from tests.integration.test_merge import FakeEngine, merge_env

pytestmark = pytest.mark.integration


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> Callable[[Exception], None]:
    def _install(error: Exception) -> None:
        monkeypatch.setattr(pdf_ops.merge, "get_engine", lambda: FakeEngine(error))

    return _install


class TestMergeRetries:
    def test_skip_second_run_is_a_no_op(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf(pages=2)
        output = out_dir / "m.pdf"

        first_code, _ = run_app(merge_env([source], output))
        assert first_code == 0
        original_bytes = output.read_bytes()
        original_mtime = output.stat().st_mtime_ns

        code, events = run_app(merge_env([source], output, PDFOPS_ON_EXISTS="skip"))

        assert code == 0
        terminal = events[-1]
        assert terminal["event"] == "operation_complete"
        assert terminal["skipped"] is True
        assert any(e["event"] == "output_skipped" for e in events)
        assert output.read_bytes() == original_bytes
        assert output.stat().st_mtime_ns == original_mtime

    def test_skip_succeeds_even_when_inputs_are_gone(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # A retry after success must be a no-op even if upstream artifacts
        # were cleaned up - the inputs are not read at all under skip.
        source = make_pdf(pages=1)
        output = out_dir / "m.pdf"
        assert run_app(merge_env([source], output))[0] == 0
        source.unlink()

        code, events = run_app(merge_env([source], output, PDFOPS_ON_EXISTS="skip"))
        assert code == 0
        assert events[-1]["skipped"] is True

    def test_skip_without_existing_output_runs_normally(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf(pages=2)
        output = out_dir / "m.pdf"
        code, events = run_app(merge_env([source], output, PDFOPS_ON_EXISTS="skip"))
        assert code == 0
        assert "skipped" not in events[-1]
        assert events[-1]["pages"] == 2
        assert output.exists()

    def test_overwrite_replaces_the_output(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        one_page = make_pdf(name="one.pdf", pages=1)
        two_pages = make_pdf(name="two.pdf", pages=2)
        output = out_dir / "m.pdf"

        assert run_app(merge_env([one_page], output))[0] == 0
        assert len(PdfReader(output).pages) == 1

        code, events = run_app(merge_env([two_pages], output, PDFOPS_ON_EXISTS="overwrite"))

        assert code == 0
        assert any(e["event"] == "output_overwritten" for e in events)
        assert len(PdfReader(output).pages) == 2
        assert sorted(p.name for p in out_dir.iterdir()) == ["m.pdf"], "no temp debris"

    def test_fail_second_run_refuses(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf()
        output = out_dir / "m.pdf"
        assert run_app(merge_env([source], output))[0] == 0
        code, events = run_app(merge_env([source], output))
        assert code == 6
        assert events[-1]["error_code"] == "OUTPUT_EXISTS"

    def test_stale_temp_from_crashed_run_is_cleaned(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf(pages=1)
        output = out_dir / "m.pdf"
        (out_dir / ".m.pdf.abc123.tmp").write_bytes(b"%PDF- crashed remnant")
        unrelated = out_dir / ".other.pdf.xyz.tmp"
        unrelated.write_bytes(b"someone else's file")

        code, events = run_app(merge_env([source], output))

        assert code == 0
        cleaned = [e for e in events if e["event"] == "stale_temp_removed"]
        assert [e["temp_file"] for e in cleaned] == [".m.pdf.abc123.tmp"]
        # scoping: another step's temp file is never touched
        assert unrelated.exists()
        assert sorted(p.name for p in out_dir.iterdir()) == [".other.pdf.xyz.tmp", "m.pdf"]


class TestExtractRetries:
    def test_skip_completes_a_partial_prior_run(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        carrier = make_pdf_with_attachments(
            [("a.txt", b"alpha"), ("b.txt", b"bravo"), ("c.txt", b"charlie")]
        )
        assert run_app(extract_env(carrier, out_dir))[0] == 0
        # simulate a crashed prior run: one file of the set is missing
        (out_dir / "b.txt").unlink()
        kept_mtime = (out_dir / "a.txt").stat().st_mtime_ns

        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="skip"))

        assert code == 0
        terminal = events[-1]
        assert terminal["attachments_extracted"] == 1  # only the missing one
        assert terminal["attachments_skipped"] == 2
        assert (out_dir / "b.txt").read_bytes() == b"bravo"
        assert (out_dir / "a.txt").stat().st_mtime_ns == kept_mtime, "existing files untouched"
        extracted = [e for e in events if e["event"] == "attachment_extracted"]
        assert [e["attachment"] for e in extracted] == ["b.txt"]

    def test_skip_full_prior_run_writes_nothing(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        carrier = make_pdf_with_attachments([("a.txt", b"alpha")])
        assert run_app(extract_env(carrier, out_dir))[0] == 0
        mtime = (out_dir / "a.txt").stat().st_mtime_ns

        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="skip"))
        assert code == 0
        assert events[-1]["attachments_extracted"] == 0
        assert events[-1]["attachments_skipped"] == 1
        assert (out_dir / "a.txt").stat().st_mtime_ns == mtime

    def test_overwrite_replaces_conflicting_files(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        carrier = make_pdf_with_attachments([("a.txt", b"fresh"), ("b.txt", b"new")])
        (out_dir / "a.txt").write_bytes(b"stale content from elsewhere")

        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="overwrite"))

        assert code == 0
        replaced = [e for e in events if e["event"] == "output_overwritten"]
        assert replaced[0]["replaced"] == ["a.txt"]
        assert (out_dir / "a.txt").read_bytes() == b"fresh"
        assert (out_dir / "b.txt").read_bytes() == b"new"


class TestDuration:
    def test_terminal_events_carry_duration(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf()
        code, events = run_app(merge_env([source], out_dir / "m.pdf"))
        assert code == 0
        assert isinstance(events[-1]["duration_s"], float)
        assert events[-1]["duration_s"] >= 0

        code, events = run_app(merge_env([out_dir / "nope.pdf"], out_dir / "m2.pdf"))
        assert code == 3
        assert "duration_s" in events[-1]


class TestStaleTempScoping:
    """The cleanup must match target names LITERALLY - glob metacharacters in
    (possibly attacker-supplied) names must not widen or narrow the match."""

    def test_metacharacter_output_name_cleans_own_temp_only(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf()
        output = out_dir / "report[1].pdf"
        own_temp = out_dir / ".report[1].pdf.abc.tmp"
        own_temp.write_bytes(b"own debris")
        foreign_temp = out_dir / ".report1.pdf.zzz.tmp"  # glob '[1]' would match this
        foreign_temp.write_bytes(b"another target's temp")

        code, events = run_app(merge_env([source], output))

        assert code == 0
        cleaned = [e["temp_file"] for e in events if e["event"] == "stale_temp_removed"]
        assert cleaned == [".report[1].pdf.abc.tmp"]
        assert foreign_temp.exists(), "a different target's temp must never be touched"
        assert output.exists()

    def test_extract_metacharacter_attachment_name(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        carrier = make_pdf_with_attachments([("b[1].txt", b"payload")])
        own_temp = out_dir / ".b[1].txt.dead.tmp"
        own_temp.write_bytes(b"debris")
        foreign_temp = out_dir / ".b1.txt.live.tmp"
        foreign_temp.write_bytes(b"other")

        code, events = run_app(extract_env(carrier, out_dir))

        assert code == 0
        cleaned = [e["temp_file"] for e in events if e["event"] == "stale_temp_removed"]
        assert cleaned == [".b[1].txt.dead.tmp"]
        assert foreign_temp.exists()
        assert (out_dir / "b[1].txt").read_bytes() == b"payload"

    def test_attachment_named_like_a_sibling_temp_survives(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # A hostile PDF can name one attachment like another's temp file;
        # the run's own planned outputs are protected from cleanup.
        carrier = make_pdf_with_attachments(
            [(".data.txt.1.tmp", b"first-payload"), ("data.txt", b"second")]
        )
        code, events = run_app(extract_env(carrier, out_dir))
        assert code == 0
        assert events[-1]["attachments_extracted"] == 2
        assert (out_dir / ".data.txt.1.tmp").read_bytes() == b"first-payload"
        assert (out_dir / "data.txt").read_bytes() == b"second"
        assert not any(e["event"] == "stale_temp_removed" for e in events)

    def test_retry_preserves_prior_attachment_named_like_a_temp(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # The retry variant: the temp-shaped attachment already sits ON DISK
        # from a completed prior run. Cleanup for the sibling target sees a
        # matching name and must recognize it as a planned output, not debris.
        carrier = make_pdf_with_attachments(
            [(".data.txt.1.tmp", b"first-payload"), ("data.txt", b"second")]
        )
        env = extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="overwrite")
        assert run_app(env)[0] == 0

        code, events = run_app(env)

        assert code == 0
        assert events[-1]["attachments_extracted"] == 2
        assert (out_dir / ".data.txt.1.tmp").read_bytes() == b"first-payload"
        assert (out_dir / "data.txt").read_bytes() == b"second"
        assert not any(e["event"] == "stale_temp_removed" for e in events)


class TestOverwriteAtomicity:
    def test_failed_overwrite_preserves_the_original(
        self,
        fake_engine: Callable[[Exception], None],
        make_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # A failing rewrite under overwrite must leave the previous output
        # byte-identical - the whole point of temp-and-rename.
        source = make_pdf()
        output = out_dir / "m.pdf"
        assert run_app(merge_env([source], output))[0] == 0
        original = output.read_bytes()

        fake_engine(
            InvalidPdfError("boom mid-rewrite", error_code=ErrorCode.CORRUPT_PDF, context={})
        )
        code, _ = run_app(merge_env([source], output, PDFOPS_ON_EXISTS="overwrite"))

        assert code == 4
        assert output.read_bytes() == original
        assert sorted(p.name for p in out_dir.iterdir()) == ["m.pdf"], "no temp debris"

    def test_extract_overwrite_replaces_via_rename_not_in_place(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        carrier = make_pdf_with_attachments([("a.txt", b"new-content")])
        conflicting = out_dir / "a.txt"
        conflicting.write_bytes(b"old")
        old_inode = conflicting.stat().st_ino

        code, _ = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="overwrite"))

        assert code == 0
        assert conflicting.read_bytes() == b"new-content"
        assert conflicting.stat().st_ino != old_inode, "must be a rename, not an in-place write"


class TestSymlinkAndDirectorySemantics:
    def test_extract_overwrite_replaces_outside_pointing_symlink(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"outside content")
        (out_dir / "report.txt").symlink_to(victim)
        carrier = make_pdf_with_attachments([("report.txt", b"fresh")])

        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="overwrite"))

        assert code == 0
        target = out_dir / "report.txt"
        assert not target.is_symlink(), "the link itself is replaced"
        assert target.read_bytes() == b"fresh"
        assert victim.read_bytes() == b"outside content", "never written through the link"
        assert any(e["event"] == "output_overwritten" for e in events)

    def test_extract_skip_completes_over_a_dangling_symlink(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # A dangling symlink is not completed prior work - skip must still
        # produce the payload (atomically replacing the link itself).
        (out_dir / "a.txt").symlink_to(tmp_path / "gone.txt")
        carrier = make_pdf_with_attachments([("a.txt", b"payload")])

        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="skip"))

        assert code == 0
        assert events[-1]["attachments_extracted"] == 1
        assert "attachments_skipped" not in events[-1] or events[-1]["attachments_skipped"] == 0
        assert (out_dir / "a.txt").read_bytes() == b"payload"

    def test_merge_skip_ignores_dangling_symlink_and_produces_output(
        self, make_pdf: Callable[..., Path], tmp_path: Path, out_dir: Path, run_app: RunApp
    ) -> None:
        output = out_dir / "m.pdf"
        output.symlink_to(tmp_path / "gone.pdf")
        source = make_pdf(pages=2)

        code, events = run_app(merge_env([source], output, PDFOPS_ON_EXISTS="skip"))

        assert code == 0
        assert "skipped" not in events[-1]
        assert not output.is_symlink()
        assert events[-1]["pages"] == 2

    @pytest.mark.parametrize("mode", ["fail", "overwrite", "skip"])
    def test_directory_at_merge_output_is_a_clean_output_error(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp, mode: str
    ) -> None:
        source = make_pdf()
        (out_dir / "m.pdf").mkdir()
        code, events = run_app(merge_env([source], out_dir / "m.pdf", PDFOPS_ON_EXISTS=mode))
        assert code == 6
        assert events[-1]["error_code"] == "OUTPUT_IS_DIRECTORY"

    def test_directory_at_extract_target_is_a_clean_output_error(
        self,
        make_pdf_with_attachments: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        carrier = make_pdf_with_attachments([("data", b"x")])
        (out_dir / "data").mkdir()
        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_ON_EXISTS="overwrite"))
        assert code == 6
        assert events[-1]["error_code"] == "OUTPUT_IS_DIRECTORY"


class TestSkipIndependence:
    def test_merge_skip_succeeds_when_password_file_is_gone(
        self,
        make_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # A retry after success reads NOTHING - not the inputs, not the
        # mounted password file (secret volumes can be gone by then too).
        source = make_pdf()
        output = out_dir / "m.pdf"
        assert run_app(merge_env([source], output))[0] == 0

        env = merge_env(
            [source],
            output,
            PDFOPS_ON_EXISTS="skip",
            PDFOPS_PASSWORD_FILE=str(tmp_path / "vanished-secret"),
        )
        code, events = run_app(env)
        assert code == 0
        assert events[-1]["skipped"] is True

    def test_config_loaded_echoes_on_exists(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf()
        code, events = run_app(merge_env([source], out_dir / "m.pdf", PDFOPS_ON_EXISTS="skip"))
        assert code == 0
        config_events = [e for e in events if e["event"] == "config_loaded"]
        assert config_events[0]["on_exists"] == "skip"
