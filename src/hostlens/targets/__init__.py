"""Hostlens execution-target abstraction.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``.

This package exposes the platform-agnostic types at the top level so the
``targets`` namespace can be imported on any host (including Windows for
schema-only / Tool Registry use cases). Concrete implementations that
depend on POSIX-only APIs — currently ``LocalTarget`` (``os.killpg`` /
``start_new_session``) and ``SSHTarget`` (only POSIX-tested) — live in
submodules and must be imported explicitly:

    from hostlens.targets.local import LocalTarget
    from hostlens.targets.ssh import SSHTarget

``hostlens.targets.local`` itself raises ``ImportError`` at module load
time on Windows hosts (per ``execution-target`` spec §场景:Windows 宿主
import 时 raise ImportError). Keeping that guard in the submodule rather
than the package ``__init__`` lets every caller decide whether they need
the concrete implementation or only the Protocol / enum / model.
"""

from __future__ import annotations

from hostlens.core.exceptions import TargetError
from hostlens.targets.base import Capability, ExecResult, ExecutionTarget
from hostlens.targets.registry import TargetRegistry

__all__ = [
    "Capability",
    "ExecResult",
    "ExecutionTarget",
    "TargetError",
    "TargetRegistry",
]
