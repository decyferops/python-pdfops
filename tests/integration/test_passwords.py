"""Password support through run(env): decrypt semantics, output-encryption
policy, and the no-leak guarantee.

The leak tests are the security contract: the literal password must never
appear in ANY output the process produces, on any path - success, failure,
or crash.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pypdf import PdfReader

import pdf_ops.merge
from pdf_ops.engine import OpenedInput
from pdf_ops.main import run
from pdf_ops.secrets import Secret
from tests.helpers import RunApp, build_raw_pdf
from tests.integration.test_extract import extract_env
from tests.integration.test_merge import merge_env

pytestmark = pytest.mark.integration

PW = "hunter2-s3cret"


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


class TestMergePasswords:
    def test_encrypted_and_plain_inputs_merge(
        self,
        make_encrypted_pdf: Callable[..., Path],
        make_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # Distinct owner password: pypdf verifies the owner password first,
        # so with the (default) owner==user setup the match reports as owner.
        locked = make_encrypted_pdf(password=PW, owner_password="owner-side-pw")
        plain = make_pdf(name="plain.pdf", pages=2)
        output = out_dir / "m.pdf"

        code, events = run_app(merge_env([locked, plain], output, PDFOPS_PASSWORD=PW))

        assert code == 0
        opened = [e for e in events if e["event"] == "input_opened"]
        assert [e["encrypted"] for e in opened] == [True, False]
        assert opened[0]["algorithm"].startswith("RC4")
        assert opened[0]["password_type"] == "user"
        assert opened[1]["algorithm"] is None
        # default policy is never: plaintext output, loud downgrade warning
        downgrades = [e for e in events if e["event"] == "security_downgrade"]
        assert len(downgrades) == 1
        assert downgrades[0]["level"] == "warning"
        assert downgrades[0]["encrypted_inputs"] == 1
        assert PdfReader(output).is_encrypted is False
        assert events[-1]["output_encrypted"] is False

    def test_aes256_input_decrypts(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password=PW, algorithm="AES-256")
        code, events = run_app(merge_env([locked], out_dir / "m.pdf", PDFOPS_PASSWORD=PW))
        assert code == 0
        opened = [e for e in events if e["event"] == "input_opened"]
        assert opened[0]["algorithm"] == "AES-256"

    @pytest.mark.parametrize(
        ("algorithm", "label"),
        [("AES-128", "AES-128"), ("RC4-40", "RC4-40")],
    )
    def test_legacy_scheme_labels_read_from_encrypt_dict(
        self,
        make_encrypted_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
        algorithm: str,
        label: str,
    ) -> None:
        # The label comes from the plaintext /Encrypt dictionary (/V, /CF),
        # not from the algorithm we happened to request - pin each branch.
        locked = make_encrypted_pdf(password=PW, algorithm=algorithm)
        code, events = run_app(merge_env([locked], out_dir / "m.pdf", PDFOPS_PASSWORD=PW))
        assert code == 0
        opened = [e for e in events if e["event"] == "input_opened"]
        assert opened[0]["algorithm"] == label

    def test_wrong_password_names_input_not_password(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        output = out_dir / "m.pdf"
        code, events = run_app(merge_env([locked], output, PDFOPS_PASSWORD="wrong-pw"))
        assert code == 5
        terminal = events[-1]
        assert terminal["error_code"] == "WRONG_PASSWORD"
        assert terminal["context"]["input"] == str(locked)
        # exact label: the failed-open scan must find the real /Encrypt
        # object, not the first /Length token some stream happens to carry
        assert terminal["context"]["algorithm"] == "RC4-128"
        assert not output.exists()

    def test_no_password_on_user_locked_input(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        code, events = run_app(merge_env([locked], out_dir / "m.pdf"))
        assert code == 5
        assert events[-1]["error_code"] == "PASSWORD_REQUIRED"

    def test_certificate_encryption_reports_unsupported(
        self, tmp_path: Path, out_dir: Path, run_app: RunApp
    ) -> None:
        # Certificate security handlers are out of scope; the operator remedy
        # (a certificate and key) is neither a password nor a repair, so the
        # classification must stay in the password class - never corrupt, and
        # never a retryable internal error.
        locked = tmp_path / "cert.pdf"
        raw = build_raw_pdf(
            [
                "<< /Type /Catalog /Pages 2 0 R >>",
                "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
                b"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s5 /V 5 /R 6 >>",
            ]
        )
        locked.write_bytes(raw.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 4 0 R"))
        code, events = run_app(merge_env([locked], out_dir / "m.pdf"))
        assert code == 5
        assert events[-1]["error_code"] == "UNSUPPORTED_ENCRYPTION"

    def test_password_required_still_reports_aes256_algorithm(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # When the file cannot be opened at all, the algorithm label comes
        # from a raw scan of the plaintext /Encrypt dictionary - the
        # observability must not disappear exactly when the operator needs it.
        locked = make_encrypted_pdf(password=PW, algorithm="AES-256")
        code, events = run_app(merge_env([locked], out_dir / "m.pdf"))
        assert code == 5
        assert events[-1]["error_code"] == "PASSWORD_REQUIRED"
        assert events[-1]["context"]["algorithm"] == "AES-256"

    def test_owner_only_file_opens_without_password(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # The common permissions-locked kind: empty user password, owner
        # password set. Every viewer opens these without a prompt; so do we.
        locked = make_encrypted_pdf(password="", owner_password="owner-pw")
        code, events = run_app(merge_env([locked], out_dir / "m.pdf"))
        assert code == 0
        opened = [e for e in events if e["event"] == "input_opened"]
        assert opened[0]["password_type"] == "empty"

    def test_password_fitting_first_but_not_second_names_second(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        first = make_encrypted_pdf(name="a.pdf", password=PW)
        second = make_encrypted_pdf(name="b.pdf", password="other-pw")
        code, events = run_app(merge_env([first, second], out_dir / "m.pdf", PDFOPS_PASSWORD=PW))
        assert code == 5
        assert events[-1]["error_code"] == "WRONG_PASSWORD"
        assert events[-1]["context"]["input"] == str(second)

    def test_password_unused_warning_on_plain_inputs(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        plain = make_pdf()
        code, events = run_app(merge_env([plain], out_dir / "m.pdf", PDFOPS_PASSWORD=PW))
        assert code == 0
        assert any(e["event"] == "password_unused" for e in events)

    def test_password_file_channel(
        self,
        make_encrypted_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        password_file = tmp_path / "pw.txt"
        password_file.write_text(PW + "\n")  # trailing newline stripped
        env = merge_env([locked], out_dir / "m.pdf", PDFOPS_PASSWORD_FILE=str(password_file))
        code, events = run_app(env)
        assert code == 0
        config_events = [e for e in events if e["event"] == "config_loaded"]
        assert config_events[0]["password"] == "set(file)"


class TestOutputEncryption:
    def test_inherit_encrypts_with_input_fallback(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        output = out_dir / "m.pdf"
        env = merge_env([locked], output, PDFOPS_PASSWORD=PW, PDFOPS_OUTPUT_ENCRYPTION="inherit")
        code, events = run_app(env)

        assert code == 0
        applied = [e for e in events if e["event"] == "output_encrypted"]
        assert applied[0]["algorithm"] == "AES-256"
        assert applied[0]["password_source"] == "input-fallback"
        assert events[-1]["output_encrypted"] is True
        assert not any(e["event"] == "security_downgrade" for e in events)

        reader = PdfReader(output)
        assert reader.is_encrypted
        assert reader.decrypt(PW) != 0  # re-locked with the same key

    def test_inherit_with_plain_inputs_stays_plain(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        plain = make_pdf()
        output = out_dir / "m.pdf"
        code, events = run_app(merge_env([plain], output, PDFOPS_OUTPUT_ENCRYPTION="inherit"))
        assert code == 0
        assert not any(e["event"] == "output_encrypted" for e in events)
        assert PdfReader(output).is_encrypted is False

    def test_always_with_explicit_output_password_file(
        self,
        make_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        plain = make_pdf()
        output = out_dir / "m.pdf"
        output_pw_file = tmp_path / "out-pw.txt"
        output_pw_file.write_text("brand-new-key")
        env = merge_env(
            [plain],
            output,
            PDFOPS_OUTPUT_ENCRYPTION="always",
            PDFOPS_OUTPUT_PASSWORD_FILE=str(output_pw_file),
        )
        code, events = run_app(env)

        assert code == 0
        applied = [e for e in events if e["event"] == "output_encrypted"]
        assert applied[0]["password_source"] == "output"
        reader = PdfReader(output)
        assert reader.is_encrypted
        assert reader.decrypt("brand-new-key") != 0

    def test_always_falls_back_to_input_password(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        output = out_dir / "m.pdf"
        env = merge_env([locked], output, PDFOPS_PASSWORD=PW, PDFOPS_OUTPUT_ENCRYPTION="always")
        code, _ = run_app(env)
        assert code == 0
        reader = PdfReader(output)
        assert reader.is_encrypted
        assert reader.decrypt(PW) != 0

    def test_inherit_after_empty_autotry_needs_explicit_password(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # The input opened with the empty password - there is no real secret
        # to inherit, and an empty-password lock would be a lock made of paper.
        locked = make_encrypted_pdf(password="", owner_password="owner-pw")
        env = merge_env([locked], out_dir / "m.pdf", PDFOPS_OUTPUT_ENCRYPTION="inherit")
        code, events = run_app(env)
        assert code == 2
        assert events[-1]["error_code"] == "MISSING_OUTPUT_PASSWORD"
        assert list(out_dir.iterdir()) == []


class TestExtractPasswords:
    def test_encrypted_extract_with_password(
        self,
        make_encrypted_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
        tmp_path: Path,
    ) -> None:
        # Build an encrypted carrier with an attachment.
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        writer.add_attachment("data.txt", b"payload")
        writer.encrypt(user_password=PW, algorithm="AES-256")
        carrier = tmp_path / "locked-carrier.pdf"
        with carrier.open("wb") as handle:
            writer.write(handle)

        code, events = run_app(extract_env(carrier, out_dir, PDFOPS_PASSWORD=PW))
        assert code == 0
        assert (out_dir / "data.txt").read_bytes() == b"payload"
        opened = [e for e in events if e["event"] == "input_opened"]
        assert opened[0]["algorithm"] == "AES-256"

    def test_wrong_password_on_extract(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        code, events = run_app(extract_env(locked, out_dir, PDFOPS_PASSWORD="nope"))
        assert code == 5
        assert events[-1]["error_code"] == "WRONG_PASSWORD"


class TestNoLeak:
    """The security contract: the literal password appears in NO output."""

    def run_and_capture_raw(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> tuple[int, str, str]:
        code = run(env)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def assert_clean(self, out: str, err: str) -> None:
        for secret in (PW, "wrong-pw", "brand-new-key"):
            assert secret not in out, f"secret {secret!r} leaked to stdout"
            assert secret not in err, f"secret {secret!r} leaked to stderr"

    def test_success_path_with_both_passwords(
        self,
        make_encrypted_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        output_pw_file = tmp_path / "opw.txt"
        output_pw_file.write_text("brand-new-key")
        env = merge_env(
            [locked],
            out_dir / "m.pdf",
            PDFOPS_PASSWORD=PW,
            PDFOPS_OUTPUT_ENCRYPTION="always",
            PDFOPS_OUTPUT_PASSWORD_FILE=str(output_pw_file),
        )
        code, out, err = self.run_and_capture_raw(env, capsys)
        assert code == 0
        self.assert_clean(out, err)

    def test_wrong_password_failure_path(
        self,
        make_encrypted_pdf: Callable[..., Path],
        out_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        env = merge_env([locked], out_dir / "m.pdf", PDFOPS_PASSWORD="wrong-pw")
        code, out, err = self.run_and_capture_raw(env, capsys)
        assert code == 5
        self.assert_clean(out, err)

    def test_crash_with_secret_in_exception_args_is_scrubbed(
        self,
        make_pdf: Callable[..., Path],
        out_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Worst case: a library exception embeds the password in its message,
        # which lands in the traceback payload. The redaction layer must
        # scrub it even there.
        class LeakyEngine:
            def open_input(self, path: Path, password: Secret | None) -> OpenedInput:
                raise RuntimeError(f"login failed for {password.reveal() if password else ''}")

            def merge_to(
                self, inputs: object, destination: Path, output_password: object
            ) -> list[str]:
                raise AssertionError("unreachable")

            def list_attachments(self, opened: object) -> list[object]:
                raise AssertionError("unreachable")

        monkeypatch.setattr(pdf_ops.merge, "get_engine", lambda: LeakyEngine())
        source = make_pdf()
        env = merge_env([source], out_dir / "m.pdf", PDFOPS_PASSWORD=PW)
        code, out, err = self.run_and_capture_raw(env, capsys)
        assert code == 1
        self.assert_clean(out, err)
        assert "login failed for ***" in out


class TestReviewHardening:
    """Behavior pinned after security review of the password machinery."""

    def test_owner_only_input_opens_even_when_a_password_is_supplied(
        self,
        make_encrypted_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # A merge can mix user-locked and permissions-locked inputs: the
        # supplied password fails on the owner-only file, but the
        # spec-standard empty try must still run before giving up.
        user_locked = make_encrypted_pdf(name="a.pdf", password=PW)
        owner_only = make_encrypted_pdf(name="b.pdf", password="", owner_password="owner-pw")
        code, events = run_app(
            merge_env([user_locked, owner_only], out_dir / "m.pdf", PDFOPS_PASSWORD=PW)
        )
        assert code == 0
        opened = [e for e in events if e["event"] == "input_opened"]
        assert [e["password_type"] for e in opened] == ["owner", "empty"]

    def test_output_password_wins_over_input_fallback(
        self,
        make_encrypted_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        locked = make_encrypted_pdf(password=PW)
        output = out_dir / "m.pdf"
        output_pw_file = tmp_path / "opw.txt"
        output_pw_file.write_text("brand-new-key")
        env = merge_env(
            [locked],
            output,
            PDFOPS_PASSWORD=PW,
            PDFOPS_OUTPUT_ENCRYPTION="inherit",
            PDFOPS_OUTPUT_PASSWORD_FILE=str(output_pw_file),
        )
        code, events = run_app(env)
        assert code == 0
        applied = [e for e in events if e["event"] == "output_encrypted"]
        assert applied[0]["password_source"] == "output"
        config_events = [e for e in events if e["event"] == "config_loaded"]
        assert config_events[0]["output_password"] == "set(file)"

        reader = PdfReader(output)
        assert reader.decrypt("brand-new-key") != 0
        fresh = PdfReader(output)
        assert fresh.decrypt(PW) == 0, "input password must NOT open the re-keyed output"

    @pytest.mark.parametrize("mode", ["inherit", "always"])
    def test_output_is_aes256_not_legacy_rc4(
        self,
        make_encrypted_pdf: Callable[..., Path],
        out_dir: Path,
        run_app: RunApp,
        mode: str,
    ) -> None:
        # The input is RC4; the output must still be AES-256 (V=5) - pypdf's
        # write default is legacy RC4, so the explicit algorithm is
        # load-bearing.
        locked = make_encrypted_pdf(password=PW)
        output = out_dir / "m.pdf"
        env = merge_env([locked], output, PDFOPS_PASSWORD=PW, PDFOPS_OUTPUT_ENCRYPTION=mode)
        code, _ = run_app(env)
        assert code == 0
        from typing import Any

        encrypt_dict: Any = PdfReader(output).trailer["/Encrypt"].get_object()
        assert int(encrypt_dict["/V"]) == 5

    def test_owner_only_with_inherit_and_explicit_output_password(
        self,
        make_encrypted_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        owner_only = make_encrypted_pdf(password="", owner_password="owner-pw")
        output = out_dir / "m.pdf"
        output_pw_file = tmp_path / "opw.txt"
        output_pw_file.write_text("fresh-key-123")
        env = merge_env(
            [owner_only],
            output,
            PDFOPS_OUTPUT_ENCRYPTION="inherit",
            PDFOPS_OUTPUT_PASSWORD_FILE=str(output_pw_file),
        )
        code, events = run_app(env)
        assert code == 0
        applied = [e for e in events if e["event"] == "output_encrypted"]
        assert applied[0]["password_source"] == "output"
        assert PdfReader(output).decrypt("fresh-key-123") != 0

    def test_owner_only_under_never_still_warns_downgrade(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # An owner-only file IS encrypted; merging it into plaintext under
        # never-mode is a downgrade worth flagging.
        owner_only = make_encrypted_pdf(password="", owner_password="owner-pw")
        code, events = run_app(merge_env([owner_only], out_dir / "m.pdf"))
        assert code == 0
        assert any(e["event"] == "security_downgrade" for e in events)

    def test_both_secrets_bad_reports_the_input_password_first(
        self,
        make_pdf: Callable[..., Path],
        tmp_path: Path,
        out_dir: Path,
        run_app: RunApp,
    ) -> None:
        # The input password resolves before the output password, so a run
        # where both sources are broken names the primary secret's problem.
        empty_pw = tmp_path / "empty-pw"
        empty_pw.write_text("")
        missing_out_pw = tmp_path / "gone-pw"
        plain = make_pdf()
        env = merge_env(
            [plain],
            out_dir / "m.pdf",
            PDFOPS_PASSWORD_FILE=str(empty_pw),
            PDFOPS_OUTPUT_ENCRYPTION="always",
            PDFOPS_OUTPUT_PASSWORD_FILE=str(missing_out_pw),
        )
        code, events = run_app(env)
        assert code == 2
        assert events[-1]["error_code"] == "EMPTY_PASSWORD"
        assert events[-1]["context"]["path"] == str(empty_pw)

    def test_short_password_degrades_redaction_with_warning(
        self, make_encrypted_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        locked = make_encrypted_pdf(password="ab")
        code, events = run_app(merge_env([locked], out_dir / "m.pdf", PDFOPS_PASSWORD="ab"))
        assert code == 0
        assert any(e["event"] == "redaction_degraded" for e in events)

    def test_common_word_password_does_not_corrupt_event_tokens(
        self, make_pdf: Callable[..., Path], out_dir: Path, run_app: RunApp
    ) -> None:
        # The regression that motivated field-restricted scrubbing: a password
        # equal to a token substring must not rewrite the event stream.
        plain = make_pdf()
        code, events = run_app(merge_env([plain], out_dir / "m.pdf", PDFOPS_PASSWORD="merge"))
        assert code == 0
        assert [e["event"] for e in events if e["event"].endswith("_written")] == ["merge_written"]
        assert all(e.get("operation") in (None, "merge") for e in events)
