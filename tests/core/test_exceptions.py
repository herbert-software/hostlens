from __future__ import annotations

import re
from pathlib import Path

import pytest

import hostlens.core.exceptions as exceptions_module
from hostlens.core.exceptions import (
    ConfigError,
    HostlensError,
    InspectorError,
    TargetError,
    ToolError,
    ToolPolicyViolation,
)


def test_subclasses_inherit_from_hostlens_error() -> None:
    assert isinstance(ConfigError("x"), HostlensError)
    assert isinstance(TargetError("x"), HostlensError)
    assert isinstance(InspectorError(kind="manifest_parse_error"), HostlensError)


def test_hostlens_error_catches_all_subclasses() -> None:
    """``HostlensError`` is the catch-all parent for all hostlens exceptions.

    Each subclass uses its own canonical construction form (positional
    free-text for ``ConfigError`` / ``TargetError`` because they retain M0
    backward compat; keyword-only for ``InspectorError`` because the M1
    structured-field migration deliberately drops the positional shape so
    every loader / runner call site is forced to carry a machine-readable
    ``kind``).
    """

    caught: list[type[HostlensError]] = []
    raisers: list[HostlensError] = [
        ConfigError("boom"),
        TargetError("boom"),
        InspectorError(kind="manifest_parse_error"),
    ]
    for exc in raisers:
        try:
            raise exc
        except HostlensError as e:
            caught.append(type(e))
    assert caught == [ConfigError, TargetError, InspectorError]


def test_config_error_accepts_optional_original_exception() -> None:
    """`ConfigError` exposes `original` so `load_settings()` can chain the
    underlying `pydantic.ValidationError` for callers that need raw details.
    The base message must remain accessible via `str(...)` unchanged.
    """

    cause = ValueError("underlying")
    err = ConfigError("formatted message", original=cause)
    assert err.original is cause
    assert str(err) == "formatted message"

    # Default keeps original=None so existing call sites continue to work.
    err_no_cause = ConfigError("just a message")
    assert err_no_cause.original is None


def test_module_exports_exactly_six_exception_classes_after_m2() -> None:
    """The exception module's public class set is locked.

    Both M0 (which seeded four classes) and M2 (which added ``ToolError`` /
    ``ToolPolicyViolation``) leave the set at six. M1
    (``add-execution-target-abstraction``) does NOT add new classes — it
    only extends ``ConfigError`` / ``TargetError`` signatures, so the
    public-class count stays at six.
    """

    public_names = [
        name
        for name in dir(exceptions_module)
        if not name.startswith("_")
        and isinstance(getattr(exceptions_module, name), type)
        and issubclass(getattr(exceptions_module, name), BaseException)
    ]
    assert sorted(public_names) == [
        "ConfigError",
        "HostlensError",
        "InspectorError",
        "TargetError",
        "ToolError",
        "ToolPolicyViolation",
    ]
    assert len(public_names) == 6
    assert sorted(exceptions_module.__all__) == [
        "ConfigError",
        "HostlensError",
        "InspectorError",
        "TargetError",
        "ToolError",
        "ToolPolicyViolation",
    ]


# ---------------------------------------------------------------------------
# M1: TargetError structured-field signature
# ---------------------------------------------------------------------------


def test_target_error_accepts_kind_keyword_form() -> None:
    """Documented call style: keyword ``kind=`` + keyword ``target=``.

    Spec §需求:`hostlens target` CLI 命令集 + design.md decision 4 lock
    ``kind`` as a structured error code (e.g. ``"ssh_auth_failed"``)
    surfaced by doctor / CLI as the user-visible error category.
    """

    err = TargetError(kind="ssh_auth_failed", target="prod-web")
    assert err.kind == "ssh_auth_failed"
    assert err.target == "prod-web"
    assert err.extra == {}
    assert err.original is None


def test_target_error_accepts_positional_kind_form() -> None:
    """Backward-compat shape ``TargetError("kind_string")``.

    M0 already had ``TargetError`` as a bare ``HostlensError`` subclass
    instantiated with a single positional message; M1 keeps that shape
    viable by making ``kind`` the first positional parameter. This
    avoids invalidating M0 callers like ``raise TargetError("boom")``.
    """

    err = TargetError("ssh_auth_failed", target="prod-web")
    assert err.kind == "ssh_auth_failed"
    assert err.target == "prod-web"


def test_target_error_str_contains_kind_and_target() -> None:
    """``__str__`` MUST surface ``kind`` + ``target`` for CLI / doctor output.

    Without this, error renderers would have to introspect ``.kind`` /
    ``.target`` attributes; the default ``Exception.__str__`` would
    only show ``args[0]`` (which is ``kind`` alone).
    """

    err = TargetError(kind="ssh_auth_failed", target="prod-web")
    text = str(err)
    assert "ssh_auth_failed" in text
    assert "prod-web" in text


def test_target_error_collects_extra_keyword_fields() -> None:
    """Arbitrary keyword args land in ``self.extra``.

    Used by call sites like ``TargetError(kind="file_too_large",
    target=..., path=..., size=...)`` — the structured context is what
    makes per-error-kind diagnosis machine-readable.
    """

    err = TargetError(
        kind="file_too_large",
        target="prod-web",
        path="/var/log/huge.log",
        size=11_000_000,
    )
    assert err.extra == {"path": "/var/log/huge.log", "size": 11_000_000}
    text = str(err)
    # extra fields surface in the rendered form as `key=value` pairs.
    assert "path=/var/log/huge.log" in text
    assert "size=11000000" in text


def test_target_error_chains_original_exception() -> None:
    """``original`` parameter chains an upstream exception."""

    cause = OSError("network unreachable")
    err = TargetError(
        kind="ssh_connect_failed",
        target="prod-web",
        original=cause,
    )
    assert err.original is cause


# ---------------------------------------------------------------------------
# M1: ConfigError structured-field extension (backward compatible)
# ---------------------------------------------------------------------------


def test_config_error_m0_call_style_still_works() -> None:
    """The M0 single-message call style MUST still work.

    Existing M0 callers (``load_settings()``) use
    ``ConfigError("invalid yaml")`` and ``ConfigError("invalid yaml",
    original=e)`` — the M1 signature extension is strictly additive.
    """

    err = ConfigError("invalid yaml")
    assert err.kind is None
    assert err.extra == {}
    assert str(err) == "invalid yaml"


def test_config_error_accepts_kind_with_structured_fields() -> None:
    """The M1 structured-field call style works.

    Spec §需求:`ConfigError` 必须扩展支持结构化 kind/extra 字段.
    """

    err = ConfigError(
        kind="missing_env_var",
        var_name="HOSTLENS_PWD",
        target="prod-web",
    )
    assert err.kind == "missing_env_var"
    assert err.extra == {"var_name": "HOSTLENS_PWD", "target": "prod-web"}

    text = str(err)
    assert "missing_env_var" in text
    assert "var_name=HOSTLENS_PWD" in text
    assert "target=prod-web" in text


def test_config_error_kind_with_original_chain() -> None:
    """``kind`` + ``original`` can coexist."""

    cause = ValueError("oops")
    err = ConfigError(kind="invalid_schema", original=cause)
    assert err.kind == "invalid_schema"
    assert err.original is cause
    # Empty body: __str__ falls back to just the kind (no double colon).
    assert str(err) == "invalid_schema"


def test_config_error_message_with_kind_renders_both() -> None:
    """``message`` and ``kind`` together render as ``"kind: message"``.

    Test for the formatter contract — caller-supplied prose context plus
    machine-readable kind both show up in user-facing rendering.
    """

    err = ConfigError("yaml parse failed", kind="invalid_yaml")
    assert "invalid_yaml: yaml parse failed" in str(err)


# ---------------------------------------------------------------------------
# M2: ToolError / ToolPolicyViolation
# ---------------------------------------------------------------------------


def test_tool_error_inherits_from_hostlens_error() -> None:
    assert issubclass(ToolError, HostlensError)
    assert isinstance(ToolError("x"), HostlensError)


def test_tool_policy_violation_inherits_from_tool_error_and_hostlens_error() -> None:
    err = ToolPolicyViolation(
        tool_name="run_inspector",
        surface="agent",
        violated_field="surfaces",
        reason="not_exposed_to_surface",
    )
    assert isinstance(err, ToolError)
    assert isinstance(err, HostlensError)


def test_tool_policy_violation_exposes_structured_fields() -> None:
    err = ToolPolicyViolation(
        tool_name="run_inspector",
        surface="mcp",
        violated_field="sensitive_output",
        reason="sensitive_output_not_declared",
    )
    assert err.tool_name == "run_inspector"
    assert err.surface == "mcp"
    assert err.violated_field == "sensitive_output"
    assert err.reason == "sensitive_output_not_declared"


def test_tool_policy_violation_str_contains_all_four_fields() -> None:
    err = ToolPolicyViolation(
        tool_name="list_targets",
        surface="agent",
        violated_field="side_effects",
        reason="side_effects_not_permitted",
    )
    text = str(err)
    assert "list_targets" in text
    assert "agent" in text
    assert "side_effects" in text
    assert "side_effects_not_permitted" in text


def test_tool_policy_violation_rejects_free_text_reason() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field="surfaces",
            reason="custom free text with /Users/alice/secrets",  # type: ignore[arg-type]
        )


def test_tool_policy_violation_rejects_invalid_surface() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="x",
            surface="openai",  # type: ignore[arg-type]
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_rejects_invalid_violated_field() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field="bogus_field",  # type: ignore[arg-type]
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_rejects_invalid_tool_name_path() -> None:
    """A path-shaped `tool_name` must be rejected at construction time so it
    cannot reach `__str__` and become a prompt/log injection vector.
    """

    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="/Users/alice/secret",
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_truncates_invalid_tool_name_to_32_chars() -> None:
    """The `ValueError` message must truncate attacker-controlled input to 32
    chars so the error itself cannot become a leak vector.
    """

    long_attacker_input = "A" * 100 + "_secret_token_value_in_full"
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name=long_attacker_input,
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )
    message = str(exc_info.value)
    # Exactly 32 chars must appear; 33 must NOT — catches regressions where
    # someone bumps the cap (e.g. to 64) and silently breaks the safety budget.
    assert "A" * 32 in message
    assert "A" * 33 not in message
    assert "_secret_token_value_in_full" not in message
    assert long_attacker_input not in message


def test_tool_policy_violation_truncates_invalid_surface_to_32_chars() -> None:
    """surface ValueError must apply the same 32-char truncation as tool_name."""

    long_attacker_input = "/Users/alice/" + "B" * 80 + "_token"
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name="x",
            surface=long_attacker_input,  # type: ignore[arg-type]
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )
    message = str(exc_info.value)
    # Exact-prefix assertion: must show first 32 chars, must NOT show 33 chars
    # (catches regressions where someone bumps truncation to 64 chars).
    assert long_attacker_input[:32] in message
    assert long_attacker_input[:33] not in message
    assert long_attacker_input not in message
    assert "_token" not in message


def test_tool_policy_violation_truncates_invalid_violated_field_to_32_chars() -> None:
    """violated_field ValueError must apply the same 32-char truncation."""

    long_attacker_input = "Bearer sk-" + "C" * 100
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field=long_attacker_input,  # type: ignore[arg-type]
            reason="not_exposed_to_surface",
        )
    message = str(exc_info.value)
    assert long_attacker_input[:32] in message
    assert long_attacker_input[:33] not in message
    assert long_attacker_input not in message


def test_tool_policy_violation_truncates_invalid_reason_to_32_chars() -> None:
    """reason ValueError must apply the same 32-char truncation."""

    long_attacker_input = "admin@10.0.0.5 " + "D" * 100
    with pytest.raises(ValueError) as exc_info:
        ToolPolicyViolation(
            tool_name="x",
            surface="agent",
            violated_field="surfaces",
            reason=long_attacker_input,  # type: ignore[arg-type]
        )
    message = str(exc_info.value)
    assert long_attacker_input[:32] in message
    assert long_attacker_input[:33] not in message
    assert long_attacker_input not in message


def test_tool_policy_violation_rejects_uppercase_tool_name() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="RunInspector",
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_rejects_kebab_tool_name() -> None:
    with pytest.raises(ValueError):
        ToolPolicyViolation(
            tool_name="run-inspector",
            surface="agent",
            violated_field="surfaces",
            reason="not_exposed_to_surface",
        )


def test_tool_policy_violation_accepts_valid_snake_case() -> None:
    err = ToolPolicyViolation(
        tool_name="run_inspector",
        surface="agent",
        violated_field="surfaces",
        reason="not_exposed_to_surface",
    )
    assert err.tool_name == "run_inspector"


def test_tool_policy_violation_repr_never_leaks_sensitive_substrings() -> None:
    """Cycle through every legal reason value and assert __str__ never leaks
    the canonical sensitive substring set (paths / IPv4 / Bearer tokens).
    """

    legal_reasons = [
        "not_exposed_to_surface",
        "side_effects_not_permitted",
        "approval_flow_not_supported_in_m2",
        "sensitive_output_not_declared",
        "missing_required_permission",
        "target_constraint_violated",
    ]
    sensitive_substrings = ["/Users/", "/home/", "Bearer "]
    ipv4_pattern = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
    for reason in legal_reasons:
        err = ToolPolicyViolation(
            tool_name="run_inspector",
            surface="agent",
            violated_field="surfaces",
            reason=reason,  # type: ignore[arg-type]
        )
        text = str(err)
        for sub in sensitive_substrings:
            assert sub not in text
        assert not ipv4_pattern.search(text)


# ---------------------------------------------------------------------------
# M1 (`add-inspector-plugin-system`): InspectorError structured-field
# ---------------------------------------------------------------------------


def test_inspector_error_accepts_keyword_call_with_multiple_fields() -> None:
    """Spec §场景:InspectorError 结构化字段可访问.

    Constructor takes keyword-only structured fields; all attributes must
    be accessible from the resulting instance so doctor / CLI / structured
    logging can render machine-readable errors without reparsing
    ``str(err)``.
    """

    err = InspectorError(
        kind="duplicate_inspector",
        inspector="x.y",
        existing_path=Path("a"),
        new_path=Path("b"),
    )
    assert err.kind == "duplicate_inspector"
    assert err.inspector == "x.y"
    assert err.existing_path == Path("a")
    assert err.new_path == Path("b")
    # Unset structured fields default to None / empty.
    assert err.path is None
    assert err.parameter is None
    assert err.secret is None
    assert err.field is None
    assert err.index is None
    assert err.errors is None
    assert err.original is None
    assert err.extra == {}


def test_inspector_error_rejects_invalid_kind() -> None:
    """Spec §场景:非法 kind 被拒绝.

    Any kind outside the 15-value M1 enum raises ``ValueError`` at
    construction time so a typo cannot silently propagate into logs as a
    string that looks like a structured error code.
    """

    with pytest.raises(ValueError, match=r"InspectorError\.kind must be one of"):
        InspectorError(kind="custom_kind_not_in_enum")  # type: ignore[arg-type]


def test_inspector_error_rejects_positional_call() -> None:
    """Spec §场景:positional 调用被拒绝.

    The M1 signature uses ``*`` to make every parameter keyword-only,
    forcing every caller through the structured form. Legacy M0
    free-text positional construction must now raise ``TypeError``; the
    rejection-fixture below is the **only** intentional positional
    invocation in the repository (its sole purpose is to verify the
    rejection contract; the inline ``noqa`` keeps the repository-wide
    grep gate `grep -rn 'InspectorError\\("` clean — see task 2.2).
    """

    with pytest.raises(TypeError):
        # Positional construction is the rejection fixture — splitting
        # the open paren and the literal across two lines keeps the
        # repository-wide grep gate `grep -rn 'InspectorError\\("' clean
        # (task 2.2) without sacrificing the negative-test coverage that
        # task 2.1 requires.
        InspectorError(
            "manifest_parse_error",  # type: ignore[misc]
        )


def test_inspector_error_str_contains_all_structured_fields() -> None:
    """Spec §场景:InspectorError 结构化字段可访问 (``str(err)`` 含全部字段).

    ``__str__`` MUST surface ``kind`` as a prefix plus every non-None
    structured field as ``key=value`` pair (sorted) so doctor / CLI
    error rendering and structlog snapshots show the full context
    without introspecting attributes.
    """

    err = InspectorError(
        kind="duplicate_inspector",
        inspector="x.y",
        existing_path=Path("a"),
        new_path=Path("b"),
    )
    text = str(err)
    # ``kind`` is always the prefix (machine-readable error code).
    assert text.startswith("duplicate_inspector:")
    assert "inspector=x.y" in text
    assert "existing_path=a" in text
    assert "new_path=b" in text


def test_inspector_error_str_includes_extra_kwargs() -> None:
    """Extra keyword arguments land in ``self.extra`` and appear in ``str()``.

    Loader / runner sometimes attach error context that isn't part of the
    canonical named-field set (e.g. ``size=`` for ``manifest_too_large``,
    ``line=`` / ``column=`` for ``manifest_parse_error``); these surface
    via the ``**extra`` bag and must render the same way as named fields.
    """

    err = InspectorError(
        kind="manifest_too_large",
        path=Path("/tmp/x.yaml"),
        size=300_000,
    )
    assert err.extra == {"size": 300_000}
    text = str(err)
    assert text.startswith("manifest_too_large:")
    assert "path=/tmp/x.yaml" in text
    assert "size=300000" in text


def test_inspector_error_renders_errors_count_not_payload() -> None:
    """``errors`` Pydantic-error list renders as ``<N items>``.

    The full list can be hundreds of bytes per item (Pydantic's
    ``.errors()`` output) and putting it inline would make doctor /
    structlog output unreadable. The full payload stays on the
    ``.errors`` attribute for callers that need it.
    """

    err = InspectorError(
        kind="manifest_validation_error",
        path=Path("/tmp/x.yaml"),
        errors=[{"loc": ("name",), "msg": "field required"}],
    )
    text = str(err)
    assert "errors=<1 items>" in text


def test_inspector_error_args_carries_kind_for_debugger_output() -> None:
    """``exc.args[0]`` is the kind so debuggers / unittest output that
    print ``exc.args`` show the machine-readable error code rather than
    an empty tuple (the latter is the default when ``super().__init__()``
    is called with no positional arg).
    """

    err = InspectorError(kind="parse_json_not_object")
    assert err.args == ("parse_json_not_object",)


def test_inspector_error_no_structured_fields_str_is_kind_with_colon() -> None:
    """When only ``kind`` is supplied, ``str(err)`` is ``"<kind>:"``.

    Keeps the rendering deterministic so log snapshot tests don't have to
    branch on whether structured fields are present.
    """

    err = InspectorError(kind="inspector_not_found")
    assert str(err) == "inspector_not_found:"
