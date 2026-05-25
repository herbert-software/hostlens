"""Tests for ``hostlens.targets.base.ExecutionTarget`` Protocol structure.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`ExecutionTarget` Protocol 必须定义完整接口.

This test file is intentionally interface-shape-only — it does NOT
exercise ``read_file`` 10MB enforcement, capability probing, env merge,
or timeout behaviour. Those are concrete-implementation contracts of
``LocalTarget`` / ``SSHTarget`` and live in tasks 3.4 / 5.6 / 5.10 tests.

Three scenarios covered here:

1. Protocol exposes **exactly** the spec-mandated members and method
   signatures (``name`` / ``type`` / ``capabilities`` attributes,
   ``exec(cmd, *, timeout, env=None)``, ``read_file(path)``).
2. ``exec`` and ``read_file`` are coroutine functions.
3. ``type`` carries the closed ``Literal["local", "ssh", "docker", "k8s"]``
   value domain.
"""

from __future__ import annotations

import inspect
import typing

from hostlens.targets.base import Capability, ExecResult, ExecutionTarget


def test_protocol_exposes_exactly_required_members() -> None:
    """Spec §场景:Protocol 形状完整.

    ``ExecutionTarget`` must surface exactly three attributes + two
    methods. Adding a member here means widening the Protocol contract;
    that requires a spec change, so this test guards against silent
    drift (e.g. a refactor adding ``async def close(self)`` would have
    to either appear in the spec or be removed from the Protocol).
    """

    annotations = ExecutionTarget.__annotations__
    assert set(annotations.keys()) == {"name", "type", "capabilities"}

    declared_callables = {
        name
        for name in vars(ExecutionTarget)
        if not name.startswith("_") and callable(vars(ExecutionTarget)[name])
    }
    assert declared_callables == {"exec", "read_file"}

    exec_sig = inspect.signature(ExecutionTarget.exec)
    parameters = exec_sig.parameters
    # First positional is `self` (Protocol descriptor), then `cmd` positional,
    # then keyword-only `timeout` and `env`.
    assert list(parameters.keys()) == ["self", "cmd", "timeout", "env"]
    assert parameters["cmd"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["timeout"].kind == inspect.Parameter.KEYWORD_ONLY
    assert parameters["env"].kind == inspect.Parameter.KEYWORD_ONLY
    assert parameters["env"].default is None

    read_sig = inspect.signature(ExecutionTarget.read_file)
    assert list(read_sig.parameters.keys()) == ["self", "path"]


def test_protocol_methods_are_coroutine_functions() -> None:
    """Spec §场景:exec 是 async 方法.

    Both Protocol methods must be coroutine functions; any concrete
    impl writing sync ``def exec`` instead of ``async def exec`` would
    be silently incompatible at runtime, so the Protocol declaration
    has to be ``async`` from the start.
    """

    assert inspect.iscoroutinefunction(ExecutionTarget.exec)
    assert inspect.iscoroutinefunction(ExecutionTarget.read_file)


def test_type_field_literal_value_domain() -> None:
    """Spec §场景:type 字段值域受限.

    The ``type`` Protocol annotation locks the value domain to the four
    target kinds defined by docs/ARCHITECTURE.md §5. Concrete classes
    expose ``type`` as a class-level constant (not a constructor kwarg);
    this test ensures the Literal includes precisely the four allowed
    strings and nothing else.
    """

    hints = typing.get_type_hints(ExecutionTarget)
    type_annotation = hints["type"]
    assert typing.get_origin(type_annotation) is typing.Literal
    assert set(typing.get_args(type_annotation)) == {"local", "ssh", "docker", "k8s"}


def test_protocol_capabilities_annotation_is_set_of_capability() -> None:
    """The ``capabilities`` field surfaces a ``set[Capability]``.

    This is a structural check — implementations may freeze the set
    (e.g. ``frozenset``) at runtime, but the Protocol annotation locks
    the element type so a callable wrong-typed implementation (returning
    ``list[str]``) is at least flagged statically by mypy.
    """

    hints = typing.get_type_hints(ExecutionTarget)
    caps_annotation = hints["capabilities"]
    # Concrete generic alias: set[Capability].
    assert typing.get_origin(caps_annotation) is set
    assert typing.get_args(caps_annotation) == (Capability,)


def test_exec_returns_exec_result_annotation() -> None:
    """``exec`` Protocol method returns ``ExecResult`` per spec annotation.

    A structural check guarding the return-type contract — concrete
    implementations are checked by mypy against this annotation.
    """

    hints = typing.get_type_hints(ExecutionTarget.exec)
    assert hints["return"] is ExecResult
