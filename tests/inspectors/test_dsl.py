"""Tests for `hostlens.inspectors.dsl` — `evaluate` / `parse_for_each` /
`format_message`.

Per spec §需求:Finding DSL 引擎 必须用 simpleeval 且禁用危险节点:

  * happy-path bound variable + builtin function (`len`)
  * attribute access on a bound variable (e.g. `p.cpu_pct > 70`)
  * lambda / list comprehension / dunder access / classic `().__class__`
    escape all raise `FeatureNotAvailable`
  * `now()` returns a tz-aware UTC `datetime`
  * `float` / `int` are registered (the `system.uptime` builtin manifest
    depends on `float(load1)` evaluating in `when:` expressions)
  * the `asyncio.wait_for` timeout path raises `asyncio.TimeoutError` on a
    slow callable (verifies the timeout mechanism, not a real ReDoS payload)

For `parse_for_each`:
  * normal `"items as x"` returns `("items", "x")`
  * missing `as` raises `InspectorError(finding_when_invalid)`
  * illegal var name raises the same kind

For `format_message`:
  * plain `"{name}"` substitution
  * `"{obj.attr}"` attribute access using `SimpleNamespace`
  * missing variable raises `KeyError` (runner catches it at the
    `format_message` call site per design.md)
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import simpleeval

from hostlens.core.exceptions import InspectorError
from hostlens.inspectors import dsl

# --------------------------------------------------------------------------- #
# evaluate — happy path
# --------------------------------------------------------------------------- #


class TestEvaluateHappyPath:
    async def test_len_function_with_bound_list(self) -> None:
        result = await dsl.evaluate("len(processes)", {"processes": [1, 2, 3]})
        assert result == 3

    async def test_attribute_access_on_bound_object(self) -> None:
        ctx = {"p": SimpleNamespace(cpu_pct=85)}
        assert await dsl.evaluate("p.cpu_pct > 70", ctx) is True

    async def test_float_is_registered(self) -> None:
        # Required by the builtin `system.uptime` manifest.
        assert await dsl.evaluate("float('4.5') > 4.0", {}) is True

    async def test_int_is_registered(self) -> None:
        assert await dsl.evaluate("int('42')", {}) == 42

    async def test_now_returns_utc_tz_aware(self) -> None:
        result = await dsl.evaluate("now()", {})
        assert isinstance(result, datetime)
        assert result.tzinfo is UTC


# --------------------------------------------------------------------------- #
# evaluate — security rejection matrix
# --------------------------------------------------------------------------- #


class TestEvaluateRejectsDangerousConstructs:
    async def test_lambda_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("(lambda: 1)()", {})

    async def test_list_comprehension_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("[x for x in [1, 2, 3]]", {})

    async def test_set_comprehension_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("{x for x in [1, 2, 3]}", {})

    async def test_dict_comprehension_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("{x: x for x in [1, 2, 3]}", {})

    async def test_generator_expression_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("sum(x for x in [1, 2, 3])", {})

    async def test_dunder_class_access_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("''.__class__.__bases__", {})

    async def test_classic_escape_subclasses_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("().__class__.__base__.__subclasses__()", {})

    async def test_dunder_import_name_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("__import__('os')", {})

    async def test_eval_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("eval('1+1')", {})

    async def test_exec_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("exec('x=1')", {})

    async def test_compile_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("compile('x', '<string>', 'eval')", {})

    async def test_open_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("open('/etc/passwd').read()", {})

    async def test_globals_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("globals()", {})

    async def test_locals_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("locals()", {})

    async def test_vars_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("vars()", {})

    async def test_dir_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("dir()", {})

    async def test_getattr_call_rejected(self) -> None:
        # ``getattr`` bypasses dunder restrictions via string indirection.
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("getattr(x, '__class__')", {})

    async def test_setattr_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("setattr(x, 'attr', 1)", {})

    async def test_delattr_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("delattr(x, 'attr')", {})

    async def test_hasattr_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("hasattr(x, '__init__')", {})

    async def test_type_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("type(x)", {})

    async def test_iter_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("iter(x)", {})

    async def test_next_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("next(x)", {})

    async def test_repr_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("repr(x)", {})

    async def test_super_call_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("super()", {})

    async def test_bare_denied_name_as_argument_rejected(self) -> None:
        # Bare denied names (e.g. ``eval`` passed as an argument rather than
        # called) must also be blocked — defense in depth against simpleeval
        # policy regressions that might let the value flow into a callable
        # which then invokes it.
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("f(eval)", {})

    async def test_bare_getattr_as_argument_rejected(self) -> None:
        with pytest.raises(simpleeval.FeatureNotAvailable):
            await dsl.evaluate("f(getattr)", {})

    async def test_len_still_works_after_denylist(self) -> None:
        # Sanity check: the deny-list must not accidentally block whitelisted
        # builtins. `len` lives in `_DSL_FUNCTIONS`, not in the deny-list.
        assert await dsl.evaluate("len(items)", {"items": [1, 2, 3]}) == 3

    async def test_normal_names_still_work_after_denylist(self) -> None:
        # Sanity check: the deny-list must not block bound user variables.
        assert await dsl.evaluate("x + 1", {"x": 5}) == 6


# --------------------------------------------------------------------------- #
# evaluate — timeout path
# --------------------------------------------------------------------------- #


class TestEvaluateTimeout:
    async def test_timeout_raises_asyncio_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Inject a stub SimpleEval whose `.eval` sleeps longer than the
        # configured timeout. The DSL's static AST gate happens before the
        # evaluator is built, so we pass an expression that parses cleanly.
        class SlowEvaluator:
            functions: ClassVar[dict[str, Any]] = {}
            names: ClassVar[dict[str, Any]] = {}

            def eval(self, expr: str) -> Any:
                del expr
                time.sleep(1.0)
                return None

        monkeypatch.setattr(dsl, "_build_evaluator", lambda _ctx: SlowEvaluator())
        with pytest.raises(asyncio.TimeoutError):
            await dsl.evaluate("1 + 1", {}, timeout_seconds=0.05)


# --------------------------------------------------------------------------- #
# parse_for_each
# --------------------------------------------------------------------------- #


class TestParseForEach:
    def test_normal_form_split_into_pair(self) -> None:
        assert dsl.parse_for_each("processes as p") == ("processes", "p")

    def test_iterable_expr_can_contain_dots(self) -> None:
        assert dsl.parse_for_each("output.rows as r") == ("output.rows", "r")

    def test_iterable_expr_can_be_function_call(self) -> None:
        assert dsl.parse_for_each("filter(rows) as r") == ("filter(rows)", "r")

    def test_missing_as_raises_finding_when_invalid(self) -> None:
        with pytest.raises(InspectorError) as exc_info:
            dsl.parse_for_each("processes p")
        assert exc_info.value.kind == "finding_when_invalid"

    def test_invalid_var_name_raises_finding_when_invalid(self) -> None:
        # `1bad` has illegal leading digit
        with pytest.raises(InspectorError) as exc_info:
            dsl.parse_for_each("processes as 1bad")
        assert exc_info.value.kind == "finding_when_invalid"

    def test_uppercase_var_name_rejected(self) -> None:
        with pytest.raises(InspectorError) as exc_info:
            dsl.parse_for_each("processes as P")
        assert exc_info.value.kind == "finding_when_invalid"


# --------------------------------------------------------------------------- #
# format_message
# --------------------------------------------------------------------------- #


class TestFormatMessage:
    def test_simple_template_substitution(self) -> None:
        assert dsl.format_message("hello {name}", {"name": "world"}) == "hello world"

    def test_attribute_access_via_simplenamespace(self) -> None:
        ctx = {"obj": SimpleNamespace(field="x")}
        assert dsl.format_message("value={obj.field}", ctx) == "value=x"

    def test_missing_variable_raises_key_error(self) -> None:
        # KeyError is intentionally allowed to propagate — the runner
        # catches it at the format_message call site.
        with pytest.raises(KeyError):
            dsl.format_message("hello {nonexistent}", {"name": "x"})
