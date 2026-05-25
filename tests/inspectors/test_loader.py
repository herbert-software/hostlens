"""Tests for `hostlens.inspectors.loader.load_manifest` and its helper
post-validators (`_validate_parameters_schema`, `_validate_command_template`,
`_validate_findings`).

The matrix is built around the four loader-level contracts mandated by the
spec: (1) safe_load + size cap + wrapped YAML errors, (2) parameter
charset constraints, (3) Jinja2 AST-based shell-injection rejection, and
(4) finding-rule consistency. Each test pins the *kind* of
`InspectorError` raised so a future loader refactor that subtly swaps
error codes is caught immediately.

The test cases for the command-template walker form the heart of the M1
shell-injection threat model — every fixture maps to a specific spec
scenario (string vs array vs subscript vs CondExpr vs block-context).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.loader import (
    _validate_command_template,
    _validate_findings,
    _validate_parameters_schema,
    load_manifest,
)
from hostlens.inspectors.schema import FindingRule

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _minimal_manifest_yaml(
    *,
    command: str = "echo hello",
    parameters_block: str = "",
    secrets_block: str = "",
    findings_block: str = "",
    parse_block: str | None = None,
    output_schema_block: str | None = None,
    extra_top: str = "",
) -> str:
    """Build a minimal valid manifest YAML string, with hooks for individual
    blocks so tests can mutate one slice at a time.
    """

    if parse_block is None:
        parse_block = "parse:\n  format: raw\n"
    if output_schema_block is None:
        output_schema_block = "output_schema:\n  type: object\n"

    parts = [
        "name: hello.echo\n",
        "version: 1.0.0\n",
        "description: echoes hello\n",
        "targets:\n  - local\n",
        f"{extra_top}",
        f"collect:\n  command: {command!r}\n",
        parse_block,
        output_schema_block,
    ]
    if parameters_block:
        parts.append(parameters_block)
    if secrets_block:
        parts.append(secrets_block)
    if findings_block:
        parts.append(findings_block)
    return "".join(parts)


def _write(tmp_path: Path, content: str, name: str = "m.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


# --------------------------------------------------------------------------- #
# Task 4.1 — load_manifest core flow
# --------------------------------------------------------------------------- #


class TestLoadManifestCore:
    def test_minimal_manifest_loads(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _minimal_manifest_yaml())
        manifest = load_manifest(path)
        assert manifest.name == "hello.echo"

    def test_file_too_large_raises(self, tmp_path: Path) -> None:
        # 256 KB + 1 byte — pad with ASCII so the file is large but YAML-safe.
        # Use comment lines so the YAML is parseable in principle.
        content = "# " + ("x" * (262_144))
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "manifest_too_large"
        assert exc.value.path == path

    def test_constructor_error_wrapped(self, tmp_path: Path) -> None:
        # safe_load rejects `!!python/object/apply` by raising
        # ConstructorError (a YAMLError subclass). Loader MUST wrap.
        content = "name: !!python/object/apply:os.system [whoami]\n"
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "manifest_parse_error"
        assert exc.value.path == path
        assert exc.value.original is not None
        # Confirm the wrapped exception is a YAMLError subclass (concretely
        # ConstructorError on PyYAML 6.x).
        import yaml as _yaml  # type: ignore[import-untyped]

        assert isinstance(exc.value.original, _yaml.YAMLError)

    def test_yaml_syntax_error_wrapped_with_line_column(self, tmp_path: Path) -> None:
        # Mismatched bracket — ScannerError / ParserError surfaces.
        content = "name: [unclosed\n"
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "manifest_parse_error"
        # line/column should be populated via problem_mark (1-indexed).
        assert exc.value.extra.get("line") is not None
        assert exc.value.extra.get("column") is not None

    def test_pydantic_validation_error_wrapped_with_errors_list(
        self, tmp_path: Path
    ) -> None:
        # `targets: []` violates `min_length=1` — Pydantic raises ValidationError.
        content = (
            "name: hello.echo\n"
            "version: 1.0.0\n"
            "description: echo\n"
            "targets: []\n"
            "collect:\n  command: 'echo hello'\n"
            "parse:\n  format: raw\n"
            "output_schema:\n  type: object\n"
        )
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "manifest_validation_error"
        assert exc.value.errors is not None
        assert len(exc.value.errors) >= 1

    def test_unclosed_template_wrapped_as_command_template_invalid(
        self, tmp_path: Path
    ) -> None:
        # `{{ unclosed` triggers TemplateSyntaxError. Loader MUST wrap.
        content = _minimal_manifest_yaml(command="{{ unclosed")
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "command_template_invalid"
        # extra.line should be populated from e.lineno.
        assert exc.value.extra.get("line") is not None

    def test_root_not_object_wrapped_as_validation_error(
        self, tmp_path: Path
    ) -> None:
        # A YAML list at root — safe_load returns a list, manifest must reject.
        content = "- hello\n- world\n"
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "manifest_validation_error"

    def test_unsafe_raw_top_level_rejected(self, tmp_path: Path) -> None:
        # `unsafe_raw: true` at top level is M1-rejected. Loader catches this
        # BEFORE Pydantic so the specific kind surfaces (rather than
        # `manifest_validation_error` for an unknown field).
        content = _minimal_manifest_yaml(extra_top="unsafe_raw: true\n")
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "unsafe_raw_not_supported_in_m1"


# --------------------------------------------------------------------------- #
# Task 4.2 — _validate_parameters_schema
# --------------------------------------------------------------------------- #


class TestValidateParametersSchema:
    def test_none_is_noop(self) -> None:
        _validate_parameters_schema(None)  # must not raise

    def test_top_level_string_without_constraint_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_parameters_schema(
                {"properties": {"host": {"type": "string"}}}
            )
        assert exc.value.kind == "parameter_missing_charset_constraint"
        assert exc.value.parameter == "host"

    def test_top_level_string_with_pattern_ok(self) -> None:
        _validate_parameters_schema(
            {"properties": {"host": {"type": "string", "pattern": "^[a-z]+$"}}}
        )

    def test_top_level_string_with_enum_ok(self) -> None:
        _validate_parameters_schema(
            {"properties": {"mode": {"type": "string", "enum": ["fast", "slow"]}}}
        )

    def test_integer_without_constraint_ok(self) -> None:
        # Integer scalars are NOT a shell-injection vector — no constraint
        # requirement applies.
        _validate_parameters_schema(
            {"properties": {"port": {"type": "integer"}}}
        )

    def test_nested_object_string_without_constraint_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_parameters_schema(
                {
                    "properties": {
                        "db": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string"},  # missing pattern/enum
                            },
                        },
                    }
                }
            )
        assert exc.value.kind == "parameter_missing_charset_constraint"
        # Path surface = `db.host` so the user can pinpoint the field.
        assert exc.value.parameter == "db.host"

    def test_array_string_items_without_constraint_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_parameters_schema(
                {
                    "properties": {
                        "endpoints": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    }
                }
            )
        assert exc.value.kind == "parameter_missing_charset_constraint"
        assert exc.value.parameter == "endpoints.items"

    def test_array_string_items_with_pattern_ok(self) -> None:
        _validate_parameters_schema(
            {
                "properties": {
                    "endpoints": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "pattern": r"^[a-zA-Z0-9.-]+:\d+$",
                        },
                    }
                }
            }
        )

    def test_array_integer_items_ok(self) -> None:
        # Array of integers — not a shell-injection vector.
        _validate_parameters_schema(
            {
                "properties": {
                    "ports": {"type": "array", "items": {"type": "integer"}}
                }
            }
        )


# --------------------------------------------------------------------------- #
# Task 4.3 — _validate_command_template
# --------------------------------------------------------------------------- #


# Reusable schema fragments.
_HOST_STRING = {"properties": {"host": {"type": "string", "pattern": r"^[a-z.]+$"}}}
_PORT_INT = {"properties": {"port": {"type": "integer"}}}
_ENDPOINTS_STR_ARR = {
    "properties": {
        "endpoints": {
            "type": "array",
            "items": {"type": "string", "pattern": r"^[a-zA-Z0-9.-]+$"},
        }
    }
}
_PORTS_INT_ARR = {
    "properties": {
        "ports": {"type": "array", "items": {"type": "integer"}}
    }
}


class TestValidateCommandTemplateString:
    """String parameters MUST flow through ``| sh``."""

    def test_a_bare_name_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("ping {{ host }}", _HOST_STRING, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "host"

    def test_b_with_sh_ok(self) -> None:
        _validate_command_template("ping {{ host | sh }}", _HOST_STRING, [])

    def test_c_default_filter_without_sh_raises(self) -> None:
        # `| default('')` does NOT count as `| sh` — the filter chain must
        # be traversed and explicit `sh` required.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ host | default('localhost') }}", _HOST_STRING, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"

    def test_default_then_sh_ok(self) -> None:
        # `| default('') | sh` IS acceptable — the chain DOES contain sh.
        _validate_command_template(
            "ping {{ host | default('localhost') | sh }}", _HOST_STRING, []
        )

    def test_g_if_block_bare_reference_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "{%- if host -%}ping {{ host }}{%- endif -%}",
                _HOST_STRING,
                [],
            )
        assert exc.value.kind == "unquoted_parameter_in_command"

    def test_o_condexpr_ternary_without_sh_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ host if host else 'localhost' }}", _HOST_STRING, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"


class TestValidateCommandTemplateNumeric:
    def test_d_integer_without_sh_ok(self) -> None:
        # Integer parameters are not shell-injection vectors.
        _validate_command_template("psql -p {{ port }}", _PORT_INT, [])


class TestValidateCommandTemplateSecrets:
    def test_e_secret_in_direct_interpolation_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "psql -W {{ PGPASSWORD }}", None, ["PGPASSWORD"]
            )
        assert exc.value.kind == "secret_inlined_in_command"
        assert exc.value.secret == "PGPASSWORD"

    def test_f_secret_in_subscript_const_raises(self) -> None:
        # `{{ env['PGPASSWORD'] }}` — secret name appears as a Const arg
        # of a Getitem expression. Loader's Pass 1 catches this.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "psql {{ env['PGPASSWORD'] }}", None, ["PGPASSWORD"]
            )
        assert exc.value.kind == "secret_inlined_in_command"

    def test_n_shell_dollar_literal_ok(self) -> None:
        # `$PGPASSWORD` is shell variable expansion, NOT Jinja2 interpolation.
        # Loader must NOT raise.
        _validate_command_template(
            "PGPASSWORD=$PGPASSWORD psql -W", None, ["PGPASSWORD"]
        )


class TestValidateCommandTemplateArray:
    def test_h_map_sh_then_join_ok(self) -> None:
        _validate_command_template(
            "ping {{ endpoints | map('sh') | join(' ') }}",
            _ENDPOINTS_STR_ARR,
            [],
        )

    def test_i_join_without_map_sh_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ endpoints | join(' ') }}", _ENDPOINTS_STR_ARR, []
            )
        assert exc.value.kind == "unquoted_array_parameter_in_command"
        assert exc.value.parameter == "endpoints"

    def test_j_filter_order_wrong_raises(self) -> None:
        # `join(...) | map('sh')` runs map on the joined string, not on
        # elements — must be rejected.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ endpoints | join(' ') | map('sh') }}",
                _ENDPOINTS_STR_ARR,
                [],
            )
        assert exc.value.kind == "unquoted_array_parameter_in_command"

    def test_k_subscript_after_array_with_sh_ok(self) -> None:
        _validate_command_template(
            "ping {{ endpoints[0] | sh }}", _ENDPOINTS_STR_ARR, []
        )

    def test_k_subscript_without_sh_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ endpoints[0] }}", _ENDPOINTS_STR_ARR, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"

    def test_l_integer_array_join_without_map_ok(self) -> None:
        _validate_command_template(
            "ports={{ ports | join(',') }}", _PORTS_INT_ARR, []
        )

    def test_p_array_missing_items_raises(self) -> None:
        # `parameters.endpoints: { type: array }` with NO `items` declaration.
        schema = {"properties": {"endpoints": {"type": "array"}}}
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ endpoints | join(' ') }}", schema, []
            )
        assert exc.value.kind == "array_parameter_items_type_undetermined"
        assert exc.value.parameter == "endpoints"

    def test_q_array_items_type_object_raises(self) -> None:
        schema = {
            "properties": {
                "endpoints": {
                    "type": "array",
                    "items": {"type": "object", "properties": {}},
                }
            }
        }
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ endpoints | join(' ') }}", schema, []
            )
        assert exc.value.kind == "array_parameter_items_type_undetermined"

    def test_r_array_items_oneof_raises(self) -> None:
        schema = {
            "properties": {
                "endpoints": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "integer"},
                        ]
                    },
                }
            }
        }
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ endpoints | join(' ') }}", schema, []
            )
        assert exc.value.kind == "array_parameter_items_type_undetermined"


class TestValidateCommandTemplateMisc:
    def test_unknown_name_does_not_raise(self) -> None:
        # A name not declared in parameters and not in secrets passes the
        # loader; Jinja2 would surface UndefinedError at render time in the
        # runner. Loader's job is to enforce the declared-surface contract,
        # not unknown-name detection.
        _validate_command_template("echo {{ unknown_var }}", None, [])


# --------------------------------------------------------------------------- #
# Task 4.4 — _validate_findings (aggregate-mode {var.attr} guard)
# --------------------------------------------------------------------------- #


class TestValidateFindings:
    def test_empty_list_noop(self) -> None:
        _validate_findings([])

    def test_for_each_rule_does_not_trigger(self) -> None:
        rule = FindingRule(
            for_each="processes as p",
            when="p.cpu > 70",
            severity="warning",
            message="{p.command} hot",
        )
        # Should not raise — for_each mode legitimately uses `{p.x}`.
        _validate_findings([rule])

    def test_aggregate_mode_no_var_attr_ok(self) -> None:
        rule = FindingRule(
            when="len(rows) > 5",
            severity="info",
            message="found {count} rows",
        )
        # The FindingRule constructor accepts this (no {var.attr} pattern).
        _validate_findings([rule])

    def test_aggregate_mode_with_var_attr_raises_at_schema_level(self) -> None:
        # FindingRule's own model_validator already rejects this at the
        # Pydantic layer (the loader-level pass exists to attach an `index`
        # field, but FindingRule's validator fires first).
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            FindingRule(
                when="len(processes) > 5",
                severity="info",
                message="Found {p.command}",
            )
        assert "finding_message_invalid_aggregate_ref" in str(exc.value)


# --------------------------------------------------------------------------- #
# Task 4.5 — end-to-end ordering: Pydantic before _validate_command_template
# --------------------------------------------------------------------------- #


class TestLoaderOrderingEnd2End:
    def test_pydantic_runs_before_command_template_walk(
        self, tmp_path: Path
    ) -> None:
        # Manifest has TWO problems: targets=[] (Pydantic) AND missing
        # sh filter (command-template). Pydantic should fire first.
        content = (
            "name: hello.echo\n"
            "version: 1.0.0\n"
            "description: echo\n"
            "targets: []\n"  # Pydantic violation
            "parameters:\n"
            "  type: object\n"
            "  properties:\n"
            "    host:\n"
            "      type: string\n"
            "      pattern: '^[a-z]+$'\n"
            "collect:\n  command: 'ping {{ host }}'\n"  # missing | sh
            "parse:\n  format: raw\n"
            "output_schema:\n  type: object\n"
        )
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        # Must be the Pydantic-layer error, not unquoted_parameter_in_command.
        assert exc.value.kind == "manifest_validation_error"

    def test_pydantic_passes_then_command_template_fires(
        self, tmp_path: Path
    ) -> None:
        # Manifest passes Pydantic but trips _validate_command_template.
        content = (
            "name: hello.echo\n"
            "version: 1.0.0\n"
            "description: echo\n"
            "targets:\n  - local\n"
            "parameters:\n"
            "  type: object\n"
            "  properties:\n"
            "    host:\n"
            "      type: string\n"
            "      pattern: '^[a-z]+$'\n"
            "collect:\n  command: 'ping {{ host }}'\n"
            "parse:\n  format: raw\n"
            "output_schema:\n  type: object\n"
        )
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "host"

    def test_pydantic_passes_then_parameters_charset_check_fires(
        self, tmp_path: Path
    ) -> None:
        # parameters-charset check fires before command-template walk.
        content = (
            "name: hello.echo\n"
            "version: 1.0.0\n"
            "description: echo\n"
            "targets:\n  - local\n"
            "parameters:\n"
            "  type: object\n"
            "  properties:\n"
            "    host:\n"
            "      type: string\n"  # missing pattern/enum
            "collect:\n  command: 'echo hello'\n"
            "parse:\n  format: raw\n"
            "output_schema:\n  type: object\n"
        )
        path = _write(tmp_path, content)
        with pytest.raises(InspectorError) as exc:
            load_manifest(path)
        assert exc.value.kind == "parameter_missing_charset_constraint"
