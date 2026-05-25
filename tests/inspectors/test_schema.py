"""Tests for `hostlens.inspectors.schema` — `InspectorManifest`,
`CollectSpec`, and `ParseSpec`.

The matrix here is intentionally exhaustive on the bits the spec calls out
by name: the strict regex-based character sets, the `extra="forbid"`
behaviour against M1-disabled fields, and the four-layer
`raw_extract_regex` static gate (length / re.compile / named-group count /
ReDoS AST walk). The ReDoS section asserts the **specific tag** surfaced
in each fixture's `ValidationError` message so that a future schema
refactor cannot silently swap one tag for another (which would weaken the
documented threat model in design.md).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)


def _valid_manifest_kwargs() -> dict[str, object]:
    """Minimal kwargs that pass every InspectorManifest validator — tests
    mutate one field at a time to assert specific failures.
    """
    return {
        "name": "hello.echo",
        "version": "1.0.0",
        "description": "echoes hello",
        "targets": ["local", "ssh"],
        "collect": CollectSpec(command="echo hello"),
        "parse": ParseSpec(format="raw"),
        "output_schema": {"type": "object"},
    }


# --------------------------------------------------------------------------- #
# InspectorManifest — strict field set
# --------------------------------------------------------------------------- #


class TestInspectorManifestFieldSet:
    def test_minimal_valid_manifest_loads(self) -> None:
        m = InspectorManifest(**_valid_manifest_kwargs())  # type: ignore[arg-type]
        assert m.name == "hello.echo"
        assert m.privilege == "none"
        assert m.tags == []
        assert m.requires_capabilities == []

    def test_extra_field_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["priority"] = "high"
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert exc_info.value.errors()[0]["type"] == "extra_forbidden"

    def test_m1_disabled_hook_field_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["hook"] = "hook.py"
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert exc_info.value.errors()[0]["type"] == "extra_forbidden"

    def test_m1_disabled_sampling_window_field_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["sampling_window"] = {"start": "now-1h"}
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    def test_m1_disabled_artifacts_field_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["artifacts"] = []
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    def test_frozen_instance_is_immutable(self) -> None:
        m = InspectorManifest(**_valid_manifest_kwargs())  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            m.name = "different.name"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# InspectorManifest — name / version regex
# --------------------------------------------------------------------------- #


class TestInspectorManifestNameVersion:
    def test_simple_name_without_dot_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["name"] = "simple_name"
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert exc_info.value.errors()[0]["type"] == "string_pattern_mismatch"

    @pytest.mark.parametrize(
        "name",
        [
            "hello.echo",
            "linux.cpu.top_processes",
            "myorg.system.uptime",
            "a.b.c.d.e",
        ],
    )
    def test_valid_multi_segment_names_accepted(self, name: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["name"] = name
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert m.name == name

    @pytest.mark.parametrize(
        "name",
        [
            "Hello.echo",  # uppercase first char
            "1hello.echo",  # leading digit
            "hello..echo",  # double dot
            ".hello.echo",  # leading dot
            "hello.echo.",  # trailing dot
            "hello-world.echo",  # hyphen in segment
        ],
    )
    def test_invalid_name_shapes_rejected(self, name: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["name"] = name
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("version", ["latest", "1", "v1.0", "1.0", "1.0.0-rc1"])
    def test_non_semver_version_rejected(self, version: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["version"] = version
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# InspectorManifest — targets
# --------------------------------------------------------------------------- #


class TestInspectorManifestTargets:
    def test_empty_targets_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["targets"] = []
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("target", ["docker", "kubernetes", "k8s", "linux"])
    def test_unknown_target_kind_rejected(self, target: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["targets"] = [target]
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    def test_local_only_accepted(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["targets"] = ["local"]
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert m.targets == ["local"]

    def test_ssh_only_accepted(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["targets"] = ["ssh"]
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert m.targets == ["ssh"]


# --------------------------------------------------------------------------- #
# InspectorManifest — requires_files
# --------------------------------------------------------------------------- #


class TestInspectorManifestRequiresFiles:
    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/x; curl evil.com",  # semicolon → out of allowlist
            "/etc/$(whoami)",  # $ → out of allowlist
            "/path with space",  # space → out of allowlist
            "/tmp/x\x00",  # NUL byte → out of allowlist
            "/etc/`whoami`",  # backtick → out of allowlist
            "/tmp/x|y",  # pipe → out of allowlist
            "/tmp/x&y",  # ampersand → out of allowlist
            "/etc/x\n",  # newline → out of allowlist
        ],
    )
    def test_shell_metachar_in_path_rejected(self, path: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["requires_files"] = [path]
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/../passwd",
            "/a/b/../c",
            "/tmp/./x",
            "/./etc/passwd",
        ],
    )
    def test_dot_or_dotdot_component_rejected(self, path: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["requires_files"] = [path]
        with pytest.raises(ValidationError):
            InspectorManifest(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/nginx/nginx.conf",
            "/usr/local/bin/foo",
            "/var/log/syslog",
            "/etc/hosts",
        ],
    )
    def test_canonical_absolute_paths_accepted(self, path: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["requires_files"] = [path]
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert path in m.requires_files


# --------------------------------------------------------------------------- #
# InspectorManifest — requires_capabilities
# --------------------------------------------------------------------------- #


class TestInspectorManifestCapabilities:
    @pytest.mark.parametrize("cap", ["shell", "file_read", "ssh", "systemd", "docker_cli"])
    def test_known_capabilities_accepted(self, cap: str) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["requires_capabilities"] = [cap]
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert cap in m.requires_capabilities

    def test_unknown_capability_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["requires_capabilities"] = ["telnet"]
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert "unknown_capability" in exc_info.value.errors()[0]["msg"]


# --------------------------------------------------------------------------- #
# InspectorManifest — output_schema
# --------------------------------------------------------------------------- #


class TestInspectorManifestOutputSchema:
    def test_non_object_top_level_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["output_schema"] = {"type": "array"}
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert "output_schema_top_level_not_object" in exc_info.value.errors()[0]["msg"]


# --------------------------------------------------------------------------- #
# CollectSpec
# --------------------------------------------------------------------------- #


class TestCollectSpec:
    def test_default_timeout_is_60(self) -> None:
        c = CollectSpec(command="echo")
        assert c.timeout_seconds == 60

    def test_timeout_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CollectSpec(command="echo", timeout_seconds=0)

    def test_timeout_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CollectSpec(command="echo", timeout_seconds=9999)

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CollectSpec(command="")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CollectSpec(command="echo", weird_field="x")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# ParseSpec — cross-field rules
# --------------------------------------------------------------------------- #


class TestParseSpecCrossFieldRules:
    def test_raw_default_accepted(self) -> None:
        p = ParseSpec(format="raw")
        assert p.columns == []
        assert p.raw_extract_regex is None

    def test_table_requires_non_empty_columns(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="table")
        assert "table_columns_required" in exc_info.value.errors()[0]["msg"]

    def test_raw_columns_without_regex_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="raw", columns=["x"])
        assert "raw_columns_require_regex" in exc_info.value.errors()[0]["msg"]

    def test_raw_regex_without_columns_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="raw", raw_extract_regex=r"(?P<x>\w+)")
        assert "raw_regex_requires_columns" in exc_info.value.errors()[0]["msg"]

    def test_json_with_columns_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="json", columns=["x"])
        assert "columns_not_allowed_for_format" in exc_info.value.errors()[0]["msg"]

    def test_kv_with_columns_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParseSpec(format="kv", columns=["x"])

    def test_json_with_non_default_skip_header_rows_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="json", skip_header_rows=2)
        assert "skip_header_rows_not_allowed" in exc_info.value.errors()[0]["msg"]

    def test_non_kv_with_non_default_delimiter_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="json", delimiter=":")
        assert "delimiter_not_allowed" in exc_info.value.errors()[0]["msg"]

    def test_non_raw_with_raw_extract_regex_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="table", columns=["x"], raw_extract_regex=r"(?P<x>\w+)")
        assert "raw_extract_regex_not_allowed" in exc_info.value.errors()[0]["msg"]

    def test_table_with_columns_and_default_skip_header_rows_accepted(self) -> None:
        p = ParseSpec(format="table", columns=["pid", "user"])
        assert p.skip_header_rows == 1

    def test_kv_with_custom_delimiter_accepted(self) -> None:
        p = ParseSpec(format="kv", delimiter=":")
        assert p.delimiter == ":"

    def test_kv_with_empty_delimiter_rejected(self) -> None:
        """Empty delimiter would crash `str.split("", maxsplit=1)` inside
        ``parse_kv``, escaping ``InspectorRunner.run()``'s "always return
        InspectorResult" contract. Reject at the schema layer instead.
        """
        with pytest.raises(ValidationError):
            ParseSpec(format="kv", delimiter="")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParseSpec(format="raw", weird_field="x")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# ParseSpec — raw_extract_regex four-layer gate
# --------------------------------------------------------------------------- #


class TestRawExtractRegexLayers:
    def test_length_above_200_rejected(self) -> None:
        # Build a >200-char regex that's otherwise valid (named group).
        body = "a" * 200
        regex = f"(?P<x>{body})"
        assert len(regex) > 200
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="raw", raw_extract_regex=regex, columns=["x"])
        assert "raw_extract_regex_too_long" in exc_info.value.errors()[0]["msg"]

    def test_non_compilable_regex_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(format="raw", raw_extract_regex="(?P<x", columns=["x"])
        assert "raw_extract_regex_invalid" in exc_info.value.errors()[0]["msg"]

    def test_anonymous_capturing_group_rejected(self) -> None:
        # Two captures: one named, one anonymous. Has no ReDoS pattern, so
        # the named-group / column-count layer should fire.
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(
                format="raw",
                raw_extract_regex=r"(\d+) (?P<y>\d+)",
                columns=["y"],
            )
        assert "raw_extract_regex_anonymous_groups_forbidden" in exc_info.value.errors()[0]["msg"]

    def test_named_group_count_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ParseSpec(
                format="raw",
                raw_extract_regex=r"(?P<a>\d+)",
                columns=["a", "b"],
            )
        assert "raw_extract_regex_column_count_mismatch" in exc_info.value.errors()[0]["msg"]

    def test_safe_regex_passes(self) -> None:
        p = ParseSpec(
            format="raw",
            raw_extract_regex=r"load: (?P<l1>[0-9.]+), (?P<l5>[0-9.]+)",
            columns=["l1", "l5"],
        )
        assert p.columns == ["l1", "l5"]


# --------------------------------------------------------------------------- #
# ParseSpec — ReDoS payload matrix
#
# Each fixture is wrapped in `(?P<x>...)` plus matching `columns=["x"]` so
# the ReDoS detection fires independently of the named-group / column-count
# layer (which would otherwise pre-empt the test). The assertion verifies
# that `ValidationError.errors()[0]["msg"]` contains the **specific** tag
# the design.md / spec.md threat model documents.
# --------------------------------------------------------------------------- #


_REDOS_FIXTURES: list[tuple[str, str, str]] = [
    ("nested_quantifier_capturing", r"(?P<x>(a+)+)", "nested_quantifier"),
    # Inner ``a*`` admits the empty match → outer repeat is over zero-width
    # matches. Spec-semantically this is the ``quantifier_on_empty_matchable``
    # category (a stricter classification than the historical
    # ``nested_quantifier`` label which only described the syntactic shape).
    ("nested_quantifier_star_star", r"(?P<x>(a*)*)", "quantifier_on_empty_matchable"),
    ("quantifier_on_empty_star_plus", r"(?P<x>(a*)+)", "quantifier_on_empty_matchable"),
    (
        "quantifier_on_empty_bounded",
        r"(?P<x>(a{0,5})+)",
        "quantifier_on_empty_matchable",
    ),
    ("nested_quantifier_non_capturing", r"(?P<x>(?:a+)+)", "nested_quantifier"),
    ("quantifier_on_lookahead", r"(?P<x>(?=a+)+a)", "quantifier_on_assert"),
    ("atomic_group", r"(?P<x>(?>a+))", "atomic_group_forbidden"),
    ("named_backreference", r"(?P<x>.+)(?P=x)+", "groupref_forbidden"),
    ("numbered_backreference", r"(?P<x>.+)\1+", "groupref_forbidden"),
    ("prefix_subset_alternation", r"(?P<x>(a|aa)+)", "prefix_subset_alternation"),
    (
        "quantifier_on_empty_matchable",
        r"(?P<x>(a?)*)",
        "quantifier_on_empty_matchable",
    ),
]


@pytest.mark.parametrize(
    "case_id,regex,expected_tag",
    _REDOS_FIXTURES,
    ids=[case[0] for case in _REDOS_FIXTURES],
)
def test_redos_fixture_rejected_with_specific_tag(
    case_id: str, regex: str, expected_tag: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ParseSpec(format="raw", raw_extract_regex=regex, columns=["x"])
    msg = exc_info.value.errors()[0]["msg"]
    assert expected_tag in msg, (
        f"ReDoS fixture {case_id!r} ({regex!r}) raised but msg does not contain "
        f"expected tag {expected_tag!r}; got msg={msg!r}"
    )


# --------------------------------------------------------------------------- #
# InspectorManifest — JSON Schema well-formedness gate
# --------------------------------------------------------------------------- #


class TestJSONSchemaWellFormedness:
    """`parameters` / `output_schema` must be valid JSON Schema documents.

    Without this gate, a bogus inline schema would pass Pydantic + loader
    and only crash at runtime inside `jsonschema.validate`, raising
    `jsonschema.exceptions.SchemaError` which the runner does not catch.
    """

    def test_bogus_parameters_type_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["parameters"] = {"type": "bogus"}
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        msg = exc_info.value.errors()[0]["msg"]
        assert "manifest_validation_error" in msg
        assert "parameters" in msg

    def test_bogus_output_schema_type_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["output_schema"] = {"type": "not_a_real_type"}
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        msg = exc_info.value.errors()[0]["msg"]
        # The output_schema_top_level_not_object validator runs first for
        # this case (type != "object"), so any error message is acceptable
        # as long as the manifest is rejected.
        assert msg

    def test_output_schema_with_invalid_inner_type_rejected(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["output_schema"] = {
            "type": "object",
            "properties": {"x": {"type": 42}},
        }
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        msg = exc_info.value.errors()[0]["msg"]
        assert "manifest_validation_error" in msg
        assert "output_schema" in msg

    def test_valid_parameters_schema_loads(self) -> None:
        kwargs = _valid_manifest_kwargs()
        kwargs["parameters"] = {
            "type": "object",
            "properties": {"host": {"type": "string", "pattern": "^[a-z]+$"}},
        }
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert m.parameters is not None

    def test_empty_output_schema_top_level_rejected_by_object_validator(
        self,
    ) -> None:
        # Empty `{}` is a valid JSON Schema (matches everything) so the
        # well-formedness gate would accept it, BUT the top-level
        # `type=object` requirement (separate validator) still rejects.
        kwargs = _valid_manifest_kwargs()
        kwargs["output_schema"] = {}
        with pytest.raises(ValidationError) as exc_info:
            InspectorManifest(**kwargs)  # type: ignore[arg-type]
        msg = exc_info.value.errors()[0]["msg"]
        assert "output_schema_top_level_not_object" in msg

    def test_empty_parameters_schema_accepted(self) -> None:
        # Empty `{}` is a valid JSON Schema — parameters has no
        # `type=object` requirement, so this loads cleanly.
        kwargs = _valid_manifest_kwargs()
        kwargs["parameters"] = {}
        m = InspectorManifest(**kwargs)  # type: ignore[arg-type]
        assert m.parameters == {}
