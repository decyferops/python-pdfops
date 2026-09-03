"""Table-driven tests for the env-var configuration contract."""

import logging
import os
from pathlib import Path

import pytest

from pdf_ops.config import (
    ExtractConfig,
    MergeConfig,
    OnExists,
    Operation,
    OutputEncryption,
    parse_config,
)
from pdf_ops.errors import ConfigError
from pdf_ops.secrets import EnvSecret, FileSecret, Secret, resolve_secret

pytestmark = pytest.mark.unit

MERGE_ENV = {
    "PDFOPS_OPERATION": "merge",
    "PDFOPS_INPUTS": "/in/a.pdf",
    "PDFOPS_OUTPUT": "/out/m.pdf",
}

EXTRACT_ENV = {
    "PDFOPS_OPERATION": "extract",
    "PDFOPS_INPUT": "/in/doc.pdf",
    "PDFOPS_OUTPUT_DIR": "/out",
}


class TestOperation:
    def test_merge_parses_to_merge_config(self) -> None:
        config = parse_config(MERGE_ENV)
        assert isinstance(config, MergeConfig)
        assert config.operation is Operation.MERGE

    def test_extract_parses_to_extract_config(self) -> None:
        config = parse_config(EXTRACT_ENV)
        assert isinstance(config, ExtractConfig)
        assert config.operation is Operation.EXTRACT
        assert config.input == Path("/in/doc.pdf")
        assert config.output_dir == Path("/out")
        assert config.fail_on_no_attachments is False

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        # Templated env values (workflow parameters, shell heredocs) often
        # carry stray whitespace; stripping it is the predictable choice.
        env = EXTRACT_ENV | {"PDFOPS_OPERATION": " extract\n"}
        assert isinstance(parse_config(env), ExtractConfig)

    @pytest.mark.parametrize("env", [{}, {"PDFOPS_OPERATION": ""}, {"PDFOPS_OPERATION": "   "}])
    def test_missing_or_empty_operation(self, env: dict[str, str]) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "MISSING_VAR"

    @pytest.mark.parametrize("value", ["bogus", "MERGE", "Merge", "merge,extract", "both"])
    def test_invalid_operation_value(self, value: str) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config({"PDFOPS_OPERATION": value})
        assert exc_info.value.error_code == "INVALID_OPERATION"
        # The operator reading the failure event must see what IS accepted.
        assert "merge" in exc_info.value.message
        assert "extract" in exc_info.value.message


class TestLogLevel:
    def test_defaults_to_info(self) -> None:
        assert parse_config(EXTRACT_ENV).log_level == logging.INFO

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("DEBUG", logging.DEBUG),
            ("debug", logging.DEBUG),
            ("Info", logging.INFO),
            ("WARNING", logging.WARNING),
            ("error", logging.ERROR),
        ],
    )
    def test_accepted_levels_case_insensitive(self, value: str, expected: int) -> None:
        assert parse_config(EXTRACT_ENV | {"PDFOPS_LOG_LEVEL": value}).log_level == expected

    @pytest.mark.parametrize("value", ["verbose", "TRACE", "42"])
    def test_invalid_level_is_config_error(self, value: str) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config(EXTRACT_ENV | {"PDFOPS_LOG_LEVEL": value})
        assert exc_info.value.error_code == "INVALID_LOG_LEVEL"


class TestMergeVars:
    def test_inputs_split_on_pathsep_in_order(self) -> None:
        env = MERGE_ENV | {"PDFOPS_INPUTS": os.pathsep.join(["/in/b.pdf", "/in/a.pdf"])}
        config = parse_config(env)
        assert isinstance(config, MergeConfig)
        assert config.inputs == (Path("/in/b.pdf"), Path("/in/a.pdf"))
        assert config.output == Path("/out/m.pdf")

    def test_component_whitespace_is_stripped(self) -> None:
        env = MERGE_ENV | {"PDFOPS_INPUTS": f" /in/a.pdf {os.pathsep} /in/b.pdf\n"}
        config = parse_config(env)
        assert isinstance(config, MergeConfig)
        assert config.inputs == (Path("/in/a.pdf"), Path("/in/b.pdf"))

    @pytest.mark.parametrize("var", ["PDFOPS_INPUTS", "PDFOPS_OUTPUT"])
    def test_merge_requires_inputs_and_output(self, var: str) -> None:
        env = dict(MERGE_ENV)
        del env[var]
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "MISSING_VAR"
        assert var in exc_info.value.message

    @pytest.mark.parametrize(
        "value",
        [
            f"/in/a.pdf{os.pathsep}",
            f"{os.pathsep}/in/a.pdf",
            f"/in/a.pdf{os.pathsep}{os.pathsep}/b",
        ],
    )
    def test_empty_path_component_rejected(self, value: str) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config(MERGE_ENV | {"PDFOPS_INPUTS": value})
        assert exc_info.value.error_code == "INVALID_INPUTS"

    def test_duplicate_inputs_rejected(self) -> None:
        # A repeated merge input is almost always a templating bug that would
        # silently duplicate content in the output document.
        value = os.pathsep.join(["/in/a.pdf", "/in/b.pdf", "/in/a.pdf"])
        with pytest.raises(ConfigError) as exc_info:
            parse_config(MERGE_ENV | {"PDFOPS_INPUTS": value})
        assert exc_info.value.error_code == "DUPLICATE_INPUTS"
        assert exc_info.value.context["duplicates"] == ["/in/a.pdf"]

    @pytest.mark.parametrize(
        "alias",
        ["/in/./a.pdf", "/in//a.pdf", "/in/a.pdf/"],
    )
    def test_duplicate_detection_sees_through_path_spelling(self, alias: str) -> None:
        # The same file spelled two ways must not slip past the check -
        # Path-level comparison catches dot segments, doubled and trailing
        # slashes (symlink aliasing is out of scope: parsing stays
        # filesystem-free).
        value = os.pathsep.join(["/in/a.pdf", alias])
        with pytest.raises(ConfigError) as exc_info:
            parse_config(MERGE_ENV | {"PDFOPS_INPUTS": value})
        assert exc_info.value.error_code == "DUPLICATE_INPUTS"

    @pytest.mark.parametrize("var", ["PDFOPS_INPUTS", "PDFOPS_OUTPUT"])
    def test_merge_vars_inapplicable_to_extract(self, var: str) -> None:
        env = EXTRACT_ENV | {var: "/some/path"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "INAPPLICABLE_VAR"
        assert var in exc_info.value.message


class TestExtractVars:
    @pytest.mark.parametrize("var", ["PDFOPS_INPUT", "PDFOPS_OUTPUT_DIR"])
    def test_extract_requires_input_and_output_dir(self, var: str) -> None:
        env = dict(EXTRACT_ENV)
        del env[var]
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "MISSING_VAR"
        assert var in exc_info.value.message

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("true", True), ("TRUE", True), ("false", False), (" False\n", False)],
    )
    def test_fail_on_no_attachments_flag(self, value: str, expected: bool) -> None:
        env = EXTRACT_ENV | {"PDFOPS_FAIL_ON_NO_ATTACHMENTS": value}
        config = parse_config(env)
        assert isinstance(config, ExtractConfig)
        assert config.fail_on_no_attachments is expected

    @pytest.mark.parametrize("value", ["yes", "1", "on", "enabled"])
    def test_invalid_flag_value_rejected(self, value: str) -> None:
        # Predictability beats convenience: only true/false are accepted, so
        # a templating mistake can't silently flip a policy.
        env = EXTRACT_ENV | {"PDFOPS_FAIL_ON_NO_ATTACHMENTS": value}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "INVALID_FLAG"

    @pytest.mark.parametrize(
        "var", ["PDFOPS_INPUT", "PDFOPS_OUTPUT_DIR", "PDFOPS_FAIL_ON_NO_ATTACHMENTS"]
    )
    def test_extract_vars_inapplicable_to_merge(self, var: str) -> None:
        env = MERGE_ENV | {var: "x"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "INAPPLICABLE_VAR"
        assert var in exc_info.value.message


class TestUnknownVars:
    def test_unknown_prefixed_var_is_rejected(self) -> None:
        env = EXTRACT_ENV | {"PDFOPS_OPERATOIN": "merge"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "UNKNOWN_VAR"
        assert "PDFOPS_OPERATOIN" in exc_info.value.message
        assert exc_info.value.context["unknown_vars"] == ["PDFOPS_OPERATOIN"]

    def test_multiple_unknown_vars_all_reported(self) -> None:
        env = EXTRACT_ENV | {"PDFOPS_ZZZ": "1", "PDFOPS_AAA": "2"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.context["unknown_vars"] == ["PDFOPS_AAA", "PDFOPS_ZZZ"]

    def test_unprefixed_vars_are_ignored(self) -> None:
        # The container inherits PATH, HOME, etc. - only our namespace is policed.
        env = EXTRACT_ENV | {"PATH": "/usr/bin", "HOME": "/home/x"}
        assert isinstance(parse_config(env), ExtractConfig)


class TestPasswordVars:
    def test_password_file_channel(self) -> None:
        config = parse_config(EXTRACT_ENV | {"PDFOPS_PASSWORD_FILE": "/secrets/pw"})
        assert isinstance(config.password, FileSecret)
        assert config.password.path == Path("/secrets/pw")

    def test_both_channels_conflict(self) -> None:
        env = EXTRACT_ENV | {"PDFOPS_PASSWORD": "x", "PDFOPS_PASSWORD_FILE": "/f"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "CONFLICTING_PASSWORD_SOURCES"

    def test_empty_password_value_counts_as_unset(self) -> None:
        config = parse_config(EXTRACT_ENV | {"PDFOPS_PASSWORD": ""})
        assert config.password is None

    def test_password_value_taken_verbatim(self) -> None:
        # Passwords may legitimately carry surrounding whitespace.
        config = parse_config(EXTRACT_ENV | {"PDFOPS_PASSWORD": " spaced pw "})
        assert isinstance(config.password, EnvSecret)
        assert config.password.value.reveal() == " spaced pw "


class TestOutputEncryptionVars:
    def test_defaults_to_never(self) -> None:
        config = parse_config(MERGE_ENV)
        assert isinstance(config, MergeConfig)
        assert config.output_encryption is OutputEncryption.NEVER
        assert config.output_password is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("never", OutputEncryption.NEVER),
            ("Inherit", OutputEncryption.INHERIT),
            ("ALWAYS", OutputEncryption.ALWAYS),
        ],
    )
    def test_modes_case_insensitive(self, value: str, expected: OutputEncryption) -> None:
        env = MERGE_ENV | {"PDFOPS_OUTPUT_ENCRYPTION": value}
        if expected is OutputEncryption.ALWAYS:
            env |= {"PDFOPS_PASSWORD": "pw"}
        config = parse_config(env)
        assert isinstance(config, MergeConfig)
        assert config.output_encryption is expected

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config(MERGE_ENV | {"PDFOPS_OUTPUT_ENCRYPTION": "sometimes"})
        assert exc_info.value.error_code == "INVALID_OUTPUT_ENCRYPTION"

    def test_output_password_with_never_is_a_hard_error(self) -> None:
        env = MERGE_ENV | {"PDFOPS_OUTPUT_PASSWORD": "x"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "OUTPUT_PASSWORD_WITHOUT_ENCRYPTION"

    def test_always_without_any_password_fails_at_parse(self) -> None:
        env = MERGE_ENV | {"PDFOPS_OUTPUT_ENCRYPTION": "always"}
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "MISSING_OUTPUT_PASSWORD"

    def test_always_with_input_password_fallback_parses(self) -> None:
        env = MERGE_ENV | {"PDFOPS_OUTPUT_ENCRYPTION": "always", "PDFOPS_PASSWORD": "pw"}
        config = parse_config(env)
        assert isinstance(config, MergeConfig)
        assert config.output_password is None  # fallback resolved at run time

    def test_conflicting_output_channels(self) -> None:
        env = MERGE_ENV | {
            "PDFOPS_OUTPUT_ENCRYPTION": "always",
            "PDFOPS_OUTPUT_PASSWORD": "a",
            "PDFOPS_OUTPUT_PASSWORD_FILE": "/f",
        }
        with pytest.raises(ConfigError) as exc_info:
            parse_config(env)
        assert exc_info.value.error_code == "CONFLICTING_PASSWORD_SOURCES"

    @pytest.mark.parametrize(
        "var", ["PDFOPS_OUTPUT_ENCRYPTION", "PDFOPS_OUTPUT_PASSWORD", "PDFOPS_OUTPUT_PASSWORD_FILE"]
    )
    def test_output_vars_inapplicable_to_extract(self, var: str) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config(EXTRACT_ENV | {var: "never" if "ENCRYPTION" in var else "x"})
        assert exc_info.value.error_code == "INAPPLICABLE_VAR"


class TestResolveSecret:
    def test_env_secret_passes_through(self) -> None:
        resolved = resolve_secret(EnvSecret(value=Secret("pw")))
        assert resolved is not None
        assert resolved.reveal() == "pw"

    def test_none_passes_through(self) -> None:
        assert resolve_secret(None) is None

    def test_file_secret_reads_and_strips_one_trailing_newline(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "pw"
        secret_file.write_text("hunter2\n")
        resolved = resolve_secret(FileSecret(path=secret_file))
        assert resolved is not None
        assert resolved.reveal() == "hunter2"

    def test_inner_whitespace_preserved(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "pw"
        secret_file.write_text(" spaced pw \r\n")
        resolved = resolve_secret(FileSecret(path=secret_file))
        assert resolved is not None
        assert resolved.reveal() == " spaced pw "

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as exc_info:
            resolve_secret(FileSecret(path=tmp_path / "nope"))
        assert exc_info.value.error_code == "PASSWORD_FILE_UNREADABLE"

    @pytest.mark.parametrize("content", ["", "\n"])
    def test_empty_file(self, tmp_path: Path, content: str) -> None:
        secret_file = tmp_path / "pw"
        secret_file.write_text(content)
        with pytest.raises(ConfigError) as exc_info:
            resolve_secret(FileSecret(path=secret_file))
        assert exc_info.value.error_code == "EMPTY_PASSWORD"


class TestPasswordHygiene:
    def test_non_utf8_password_file_is_config_error_without_detail(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "pw"
        secret_file.write_bytes(b"\x80\x81secret-bytes\xff")
        with pytest.raises(ConfigError) as exc_info:
            resolve_secret(FileSecret(path=secret_file))
        assert exc_info.value.error_code == "PASSWORD_FILE_UNREADABLE"
        # no byte values or offsets from the decode failure may surface
        assert "0x80" not in exc_info.value.message
        assert "position" not in exc_info.value.message

    @pytest.mark.parametrize("bad", ["pw\x01probe", "pw\x7fhidden", "pw\x85nel"])
    def test_control_characters_rejected_env_channel(self, bad: str) -> None:
        with pytest.raises(ConfigError) as exc_info:
            resolve_secret(EnvSecret(value=Secret(bad)))
        assert exc_info.value.error_code == "PASSWORD_UNSUPPORTED_CHARACTERS"
        assert "\x01" not in exc_info.value.message

    def test_control_characters_rejected_file_channel(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "pw"
        secret_file.write_text("pw\x01probe\n")
        with pytest.raises(ConfigError) as exc_info:
            resolve_secret(FileSecret(path=secret_file))
        assert exc_info.value.error_code == "PASSWORD_UNSUPPORTED_CHARACTERS"


class TestEmptyInapplicableVars:
    @pytest.mark.parametrize("var", ["PDFOPS_INPUTS", "PDFOPS_OUTPUT_ENCRYPTION"])
    def test_empty_valued_inapplicable_var_is_ignored(self, var: str) -> None:
        # empty equals missing - an empty variable configures nothing, so it
        # cannot be an inapplicability conflict either
        config = parse_config(EXTRACT_ENV | {var: ""})
        assert isinstance(config, ExtractConfig)


class TestOnExists:
    def test_defaults_to_fail(self) -> None:
        assert parse_config(MERGE_ENV).on_exists is OnExists.FAIL
        assert parse_config(EXTRACT_ENV).on_exists is OnExists.FAIL

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("fail", OnExists.FAIL), ("Overwrite", OnExists.OVERWRITE), ("SKIP", OnExists.SKIP)],
    )
    def test_modes_case_insensitive(self, value: str, expected: OnExists) -> None:
        assert parse_config(MERGE_ENV | {"PDFOPS_ON_EXISTS": value}).on_exists is expected

    def test_applies_to_both_operations(self) -> None:
        config = parse_config(EXTRACT_ENV | {"PDFOPS_ON_EXISTS": "skip"})
        assert config.on_exists is OnExists.SKIP

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            parse_config(MERGE_ENV | {"PDFOPS_ON_EXISTS": "maybe"})
        assert exc_info.value.error_code == "INVALID_ON_EXISTS"
