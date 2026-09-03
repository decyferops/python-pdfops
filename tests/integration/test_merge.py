"""End-to-end merge runs through run(env): the merge operator contract."""

import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pypdf import PdfReader

import pdf_ops.merge
from pdf_ops.engine import OpenedInput
from pdf_ops.errors import ErrorCode, InvalidPdfError
from tests.helpers import RunApp, build_raw_pdf

pytestmark = pytest.mark.integration


def merge_env(inputs: list[Path], output: Path, **extra: str) -> dict[str, str]:
    return {
        "PDFOPS_OPERATION": "merge",
        "PDFOPS_INPUTS": os.pathsep.join(str(p) for p in inputs),
        "PDFOPS_OUTPUT": str(output),
        **extra,
    }


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


class TestMergeSuccess:
    def test_two_files_merge_in_order(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        first = make_pdf(name="a.pdf", pages=2, page_width=111.0)
        second = make_pdf(name="b.pdf", pages=3, page_width=222.0)
        output = out_dir / "merged.pdf"

        code, events = run_app(merge_env([first, second], output))

        assert code == 0
        assert [e["event"] for e in events] == [
            "config_loaded",
            "operation_started",
            "input_opened",
            "input_opened",
            "merge_written",
            "operation_complete",
        ]
        opened = [e for e in events if e["event"] == "input_opened"]
        assert [e["encrypted"] for e in opened] == [False, False]
        assert events[-2]["pages_per_input"] == [2, 3]
        assert events[-2]["output_encrypted"] is False
        terminal = events[-1]
        assert terminal["event"] == "operation_complete"
        assert terminal["inputs_merged"] == 2
        assert terminal["pages"] == 5
        assert terminal["bytes_written"] == output.stat().st_size
        assert terminal["output_path"] == str(output)
        # The rename is the publish step: nothing but the final file remains.
        assert sorted(out_dir.iterdir()) == [output]

        reader = PdfReader(output)
        assert len(reader.pages) == 5
        widths = [round(float(page.mediabox.width)) for page in reader.pages]
        assert widths == [111, 111, 222, 222, 222]

    def test_input_order_is_the_env_order(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        first = make_pdf(name="a.pdf", pages=1, page_width=111.0)
        second = make_pdf(name="b.pdf", pages=1, page_width=222.0)
        output = out_dir / "merged.pdf"

        code, _ = run_app(merge_env([second, first], output))

        assert code == 0
        widths = [round(float(p.mediabox.width)) for p in PdfReader(output).pages]
        assert widths == [222, 111]

    def test_single_input_is_a_valid_merge(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # Degenerate but legitimate: workflows fan in a variable-length list
        # that can be of length one.
        single = make_pdf(pages=2)
        output = out_dir / "merged.pdf"
        code, events = run_app(merge_env([single], output))
        assert code == 0
        assert events[-1]["pages"] == 2


class TestInputValidation:
    def test_all_problems_reported_in_one_run(
        self, tmp_path: Path, out_dir: Path, run_app: RunApp
    ) -> None:
        missing_one = tmp_path / "nope1.pdf"
        missing_two = tmp_path / "nope2.pdf"
        code, events = run_app(merge_env([missing_one, missing_two], out_dir / "m.pdf"))
        assert code == 3
        terminal = events[-1]
        assert terminal["error_code"] == "INPUT_MISSING"
        assert [p["input"] for p in terminal["context"]["problems"]] == [
            str(missing_one),
            str(missing_two),
        ]

    def test_first_problem_determines_exit_class(
        self,
        make_non_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        fake = make_non_pdf()
        missing = tmp_path / "nope.pdf"
        code, events = run_app(merge_env([fake, missing], out_dir / "m.pdf"))
        assert code == 4  # first problem is NOT_A_PDF -> invalid-PDF class
        assert events[-1]["error_code"] == "NOT_A_PDF"
        assert len(events[-1]["context"]["problems"]) == 2

    def test_directory_input_rejected(self, tmp_path: Path, out_dir: Path, run_app: RunApp) -> None:
        directory = tmp_path / "a-directory.pdf"
        directory.mkdir()
        code, events = run_app(merge_env([directory], out_dir / "m.pdf"))
        assert code == 3
        assert events[-1]["error_code"] == "INPUT_IS_DIRECTORY"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permissions")
    def test_unreadable_input_rejected(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_pdf(name="locked.pdf")
        locked.chmod(0o000)
        try:
            code, events = run_app(merge_env([locked], out_dir / "m.pdf"))
        finally:
            locked.chmod(0o644)
        assert code == 3
        assert events[-1]["error_code"] == "INPUT_UNREADABLE"

    def test_empty_file_is_not_a_pdf(
        self, make_non_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        empty = make_non_pdf(name="empty.pdf", content=b"")
        code, events = run_app(merge_env([empty], out_dir / "m.pdf"))
        assert code == 4
        assert events[-1]["error_code"] == "NOT_A_PDF"

    @pytest.mark.parametrize("mode", ["garbage-body", "no-objects"])
    def test_corrupt_pdf_rejected(
        self,
        make_corrupt_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
        mode: str,
    ) -> None:
        corrupt = make_corrupt_pdf(mode=mode)
        code, events = run_app(merge_env([corrupt], out_dir / "m.pdf"))
        assert code == 4
        assert events[-1]["error_code"] == "CORRUPT_PDF"


class TestAtomicity:
    def test_corrupt_second_input_leaves_no_trace_in_output_dir(
        self,
        make_pdf: Callable[..., Path],
        make_corrupt_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        good = make_pdf(name="good.pdf")
        corrupt = make_corrupt_pdf()
        output = out_dir / "merged.pdf"

        code, events = run_app(merge_env([good, corrupt], output))

        assert code == 4
        assert events[-1]["error_code"] == "CORRUPT_PDF"
        assert not output.exists()
        assert list(out_dir.iterdir()) == [], "no partial or temp files may remain"

    def test_output_mode_honors_umask_not_mkstemp_0600(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # mkstemp creates the temp 0600 and the rename would carry that onto
        # the published output - unreadable by a downstream step running as a
        # different UID on a shared volume. The output must get what a plain
        # open() would under the current umask.
        source = make_pdf()
        output = out_dir / "m.pdf"
        assert run_app(merge_env([source], output))[0] == 0
        umask = os.umask(0)
        os.umask(umask)
        assert output.stat().st_mode & 0o777 == 0o666 & ~umask


class TestOutputPolicy:
    def test_existing_output_refused(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        existing = out_dir / "merged.pdf"
        existing.write_bytes(b"%PDF- pre-existing")
        source = make_pdf()

        code, events = run_app(merge_env([source], existing))

        assert code == 6
        assert events[-1]["error_code"] == "OUTPUT_EXISTS"
        assert existing.read_bytes() == b"%PDF- pre-existing", "existing output untouched"

    def test_missing_output_dir_refused(
        self, make_pdf: Callable[..., Path], tmp_path: Path, run_app: RunApp
    ) -> None:
        source = make_pdf()
        code, events = run_app(merge_env([source], tmp_path / "no-such-dir" / "m.pdf"))
        assert code == 6
        assert events[-1]["error_code"] == "OUTPUT_DIR_MISSING"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permissions")
    def test_unwritable_output_dir_refused(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        source = make_pdf()
        out_dir.chmod(0o500)
        try:
            code, events = run_app(merge_env([source], out_dir / "m.pdf"))
        finally:
            out_dir.chmod(0o755)
        assert code == 6
        assert events[-1]["error_code"] == "OUTPUT_NOT_WRITABLE"


class TestPathologicalInputs:
    def test_catalog_without_pages_maps_to_corrupt(
        self, make_pathological_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # Valid header + xref but no /Pages: a structural hole the engine
        # must classify as exit 4, never as an internal error.
        bad = make_pathological_pdf()
        code, events = run_app(merge_env([bad], out_dir / "m.pdf"))
        assert code == 4
        assert events[-1]["error_code"] == "CORRUPT_PDF"

    def test_library_warning_goes_to_stdout_json_not_stderr(
        self, make_dangling_ref_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # A repairable input: qpdf fixes it up but reports the damage. The
        # run_app invariants assert stderr stays empty and stdout stays
        # JSON-only; the warning must surface as a JSON event.
        repairable = make_dangling_ref_pdf()
        code, events = run_app(merge_env([repairable], out_dir / "m.pdf"))
        assert code == 0
        warnings = [e for e in events if e["event"] == "pdf_library_message"]
        assert warnings, "the library warning must appear as a structured event"
        assert warnings[0]["level"] == "warning"
        assert "repairing" in warnings[0]["detail"]
        assert warnings[0]["source"] == "qpdf"

    def test_repair_during_lazy_write_still_surfaces_a_warning(
        self, tmp_path: Path, out_dir: Path, run_app: RunApp
    ) -> None:
        # qpdf reads stream data lazily: damage discovered only while the
        # writer copies (a wrong stream /Length) repairs at write time and
        # must still surface as an event, not be silently absorbed.
        source = tmp_path / "late.pdf"
        source.write_bytes(
            build_raw_pdf(
                [
                    "<< /Type /Catalog /Pages 2 0 R >>",
                    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Contents 4 0 R >>",
                    b"<< /Length 999 >>\nstream\n0 0 m 10 10 l S\nendstream",
                ]
            )
        )
        code, events = run_app(merge_env([source], out_dir / "m.pdf"))
        assert code == 0
        warnings = [e for e in events if e["event"] == "pdf_library_message"]
        assert warnings, "a write-time repair must produce a structured event"

    def test_light_damage_is_repaired_with_warnings(
        self,
        make_damaged_pdf: Callable[..., Path],
        make_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # A mangled xref is recoverable: the engine reconstructs the table,
        # the merge succeeds with every page, and the damage is visible in
        # the log rather than silently absorbed.
        damaged = make_damaged_pdf()
        plain = make_pdf(name="plain.pdf")
        output = out_dir / "m.pdf"
        code, events = run_app(merge_env([damaged, plain], output))
        assert code == 0
        assert events[-1]["pages"] == 3
        assert any(e["event"] == "pdf_library_message" for e in events)
        assert len(PdfReader(output).pages) == 3


class FakeEngine:
    """Writes bytes to the temp destination, then fails - exercises cleanup
    of a non-empty temp file, which no real parse failure reaches."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def open_input(self, path: Path, password: object) -> OpenedInput:
        return OpenedInput(
            path=path, handle=None, pages=1, encrypted=False, algorithm=None, password_type=None
        )

    def merge_to(self, inputs: object, destination: Path, output_password: object) -> list[str]:
        destination.write_bytes(b"%PDF- partial garbage")
        raise self.error


class TestFailureAfterBytesWritten:
    @pytest.fixture
    def fake_engine(self, monkeypatch: pytest.MonkeyPatch) -> Callable[[Exception], None]:
        def _install(error: Exception) -> None:
            monkeypatch.setattr(pdf_ops.merge, "get_engine", lambda: FakeEngine(error))

        return _install

    def test_disk_full_translated_and_temp_cleaned(
        self,
        fake_engine: Callable[[Exception], None],
        make_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        fake_engine(OSError(errno.ENOSPC, "No space left on device"))
        source = make_pdf()
        code, events = run_app(merge_env([source], out_dir / "m.pdf"))
        assert code == 6
        assert events[-1]["error_code"] == "DISK_FULL"
        assert list(out_dir.iterdir()) == [], "temp file with bytes must be cleaned up"

    def test_app_error_after_partial_write_cleans_temp(
        self,
        fake_engine: Callable[[Exception], None],
        make_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        fake_engine(InvalidPdfError("boom mid-write", error_code=ErrorCode.CORRUPT_PDF, context={}))
        source = make_pdf()
        code, _ = run_app(merge_env([source], out_dir / "m.pdf"))
        assert code == 4
        assert list(out_dir.iterdir()) == []
