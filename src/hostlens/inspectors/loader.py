"""Inspector manifest YAML loader.

`load_manifest` is the single entry point. It enforces five layers of static
contract before returning an `InspectorManifest` (see
`inspector-plugin-system/spec.md` for the full requirement set):

  1. Size cap (256 KB) — reject pathological YAML before parsing.
  2. `yaml.safe_load` — never `yaml.load`; reject any `yaml.YAMLError`
     subclass (including `ConstructorError` from RCE-shaped tag exploits)
     by wrapping into `InspectorError(kind="manifest_parse_error")`.
  3. Pydantic v2 schema validation via `InspectorManifest.model_validate`;
     `ValidationError` wrapped to `manifest_validation_error`.
  4. `parameters` JSON Schema walk — every string field (top-level / nested
     object / array(string-items)) MUST declare `pattern` or `enum`,
     otherwise the field is a shell-injection vector.
  5. `collect.command` Jinja2 AST walk — string / array(string-items)
     parameter references MUST flow through the `sh` filter; secret names
     MUST NOT appear in interpolation position; `unsafe_raw: true` is
     M1-rejected.

Every failure surfaces as `InspectorError(kind=..., ...)` — Jinja2 /
Pydantic / PyYAML / `simpleeval` exceptions never propagate out of the
loader (callers rely on a closed error vocabulary for `doctor` /
structured logging).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
import yaml
from jinja2 import nodes
from pydantic import ValidationError

from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.schema import FindingRule, InspectorManifest

__all__ = ["load_manifest"]


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


_MAX_MANIFEST_BYTES = 262_144  # 256 KB


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def load_manifest(path: Path) -> InspectorManifest:
    """Load and fully validate an Inspector manifest from ``path``.

    Raises ``InspectorError`` with a closed-set ``kind`` on any failure;
    PyYAML / Pydantic / Jinja2 exceptions are wrapped, never propagated.
    """

    # ---- 1. Size cap ----
    size = path.stat().st_size
    if size > _MAX_MANIFEST_BYTES:
        raise InspectorError(
            kind="manifest_too_large",
            path=path,
            size=size,
        )

    # ---- 2. YAML parse (safe_load) ----
    raw = path.read_text()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        line: int | None = None
        column: int | None = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            # `problem_mark.line` / `.column` are 0-indexed in PyYAML.
            line = int(mark.line) + 1
            column = int(mark.column) + 1
        raise InspectorError(
            kind="manifest_parse_error",
            path=path,
            original=exc,
            line=line,
            column=column,
        ) from exc

    if not isinstance(data, dict):
        # `safe_load` of an empty file returns None; of a scalar / list returns
        # a non-dict — none of these can be a valid manifest. Surface as
        # manifest_validation_error so users see a consistent error vocabulary.
        raise InspectorError(
            kind="manifest_validation_error",
            path=path,
            errors=[
                {
                    "type": "manifest_not_object",
                    "msg": f"manifest root must be a YAML mapping, got {type(data).__name__}",
                }
            ],
        )

    # ---- M1 explicit reject: unsafe_raw opt-out is not supported ----
    if data.get("unsafe_raw") is True:
        raise InspectorError(
            kind="unsafe_raw_not_supported_in_m1",
            path=path,
        )

    # ---- 3. Pydantic schema validation ----
    try:
        manifest = InspectorManifest.model_validate(data)
    except ValidationError as exc:
        # Pydantic's `errors()` returns a typed `list[ErrorDetails]`; cast to
        # the looser `list[dict[str, Any]]` that `InspectorError` accepts so
        # the structured-field shape is uniform across all loader error sites.
        raise InspectorError(
            kind="manifest_validation_error",
            path=path,
            errors=[dict(err) for err in exc.errors()],
        ) from exc

    # ---- 4. parameters JSON Schema walk ----
    _validate_parameters_schema(manifest.parameters)

    # ---- 5. Jinja2 AST walk on collect.command ----
    _validate_command_template(
        manifest.collect.command,
        manifest.parameters,
        manifest.secrets,
        path=path,
    )

    # ---- 6. Manifest-level finding consistency checks ----
    _validate_findings(manifest.findings)

    return manifest


# --------------------------------------------------------------------------- #
# `_validate_parameters_schema`
# --------------------------------------------------------------------------- #


# String fields that constitute a shell-injection surface MUST carry one of
# these two keys. `const` is intentionally NOT honoured — a literal value
# bypassing user input is still allowed via the manifest itself, not via the
# parameters schema.
_STRING_CONSTRAINT_KEYS: frozenset[str] = frozenset({"pattern", "enum"})


def _validate_parameters_schema(parameters: dict[str, Any] | None) -> None:
    """Recursively walk a JSON Schema ``parameters`` dict and reject any
    string / array(string-items) field that lacks a ``pattern`` or
    ``enum`` constraint.

    Walks ``properties`` of every ``type: object`` schema (including nested
    objects); enters ``items`` of every ``type: array``. Non-string scalar
    types (``integer`` / ``number`` / ``boolean``) impose no constraint
    requirement because they cannot be shell-evaluated as text.
    """

    if parameters is None:
        return

    # Top-level must be an object schema (enforced upstream by Pydantic for
    # the manifest, but `parameters` itself is `dict[str, Any] | None` so
    # we re-check defensively here).
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return

    for name, schema in properties.items():
        if isinstance(schema, dict):
            _validate_property(name, schema)


def _validate_property(path: str, schema: dict[str, Any]) -> None:
    """Validate a single JSON Schema property at ``path`` (used for error
    surface — e.g. ``endpoints.items`` for the items schema of an array).
    """

    schema_type = schema.get("type")

    if schema_type == "string":
        if not (_STRING_CONSTRAINT_KEYS & schema.keys()):
            raise InspectorError(
                kind="parameter_missing_charset_constraint",
                parameter=path,
            )
        return

    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            items_type = items.get("type")
            if items_type == "string":
                # Recurse with `<path>.items` so the error message says e.g.
                # `parameter="endpoints.items"` (per spec).
                _validate_property(f"{path}.items", items)
            elif items_type == "object":
                _validate_property(f"{path}.items", items)
        # Array items with no `type` / oneOf / etc. — that's a problem for the
        # command-template walker (it cannot decide whether elements need
        # `| map('sh') | join`), but not for the constraint walker; the
        # template walker rejects such manifests downstream.
        return

    if schema_type == "object":
        nested = schema.get("properties")
        if isinstance(nested, dict):
            for nested_name, nested_schema in nested.items():
                if isinstance(nested_schema, dict):
                    _validate_property(f"{path}.{nested_name}", nested_schema)


# --------------------------------------------------------------------------- #
# `_validate_command_template`
# --------------------------------------------------------------------------- #


def _build_parent_map(root: nodes.Node) -> dict[int, nodes.Node]:
    """Return ``{id(child): parent}`` for every node reachable from ``root``.

    We can't store parents on the AST nodes directly (Jinja2 nodes don't
    accept arbitrary attribute assignment under `__slots__`-ish semantics
    in some versions), so we key by ``id()`` — which is stable for the
    lifetime of the parsed tree we hold in this function.
    """

    parents: dict[int, nodes.Node] = {}

    def _walk(node: nodes.Node) -> None:
        for child in node.iter_child_nodes():
            parents[id(child)] = node
            _walk(child)

    _walk(root)
    return parents


def _filter_chain(
    name_node: nodes.Name,
    parents: dict[int, nodes.Node],
) -> list[nodes.Filter]:
    """Return the chain of ``Filter`` nodes wrapping ``name_node``, ordered
    innermost-first.

    Stops at the first non-Filter ancestor. For example::

        {{ host | default("") | sh }}

    yields ``[Filter(default), Filter(sh)]`` (inner→outer).
    """

    chain: list[nodes.Filter] = []
    current: nodes.Node = name_node
    while True:
        parent = parents.get(id(current))
        if parent is None or not isinstance(parent, nodes.Filter):
            break
        # Filter wraps a single inner expression via `.node`. If we're not
        # the `.node` of this Filter (we're e.g. one of its args), stop —
        # the filter isn't being applied to us.
        if parent.node is not current:
            break
        chain.append(parent)
        current = parent
    return chain


def _chain_contains_sh(chain: list[nodes.Filter]) -> bool:
    return any(f.name == "sh" for f in chain)


def _chain_is_map_sh_then_join(chain: list[nodes.Filter]) -> bool:
    """True iff the filter chain is exactly the array-string-items pattern::

        {{ <name> | map('sh') | join(<delim>) }}

    (innermost first → ``[map, join]``). The chain may contain additional
    filters wrapping ``join``, but ``map('sh')`` must come **before**
    ``join`` and ``join`` must be present.
    """

    map_index: int | None = None
    join_index: int | None = None
    for i, f in enumerate(chain):
        if f.name == "map" and map_index is None:
            # `map('sh')` — first positional arg must be the const 'sh'.
            if len(f.args) >= 1 and isinstance(f.args[0], nodes.Const) and f.args[0].value == "sh":
                map_index = i
        elif f.name == "join" and join_index is None:
            join_index = i
    return map_index is not None and join_index is not None and map_index < join_index


def _param_type_lookup(
    parameters: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Flatten ``parameters.properties`` into ``{name: property_schema}``."""

    if parameters is None:
        return {}
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {k: v for k, v in properties.items() if isinstance(v, dict)}


def _classify_array_items(
    array_schema: dict[str, Any],
    parameter_name: str,
) -> str:
    """Classify the element type of an ``array``-typed parameter.

    Returns one of:
      * ``"string"``    — items.type == 'string'
      * ``"numeric"``   — items.type in {integer, number, boolean}
      * ``"undetermined"`` — items missing / no type / type is object|array /
        items uses oneOf/anyOf/allOf

    Per spec, ``undetermined`` MUST be rejected by the caller (it's a
    shell-injection bypass surface).
    """

    items = array_schema.get("items")
    if not isinstance(items, dict):
        return "undetermined"
    if any(key in items for key in ("oneOf", "anyOf", "allOf")):
        return "undetermined"
    items_type = items.get("type")
    if items_type == "string":
        return "string"
    if items_type in ("integer", "number", "boolean"):
        return "numeric"
    # Missing type, or type in {"object", "array"} — undetermined.
    return "undetermined"


def _validate_command_template(
    command: str,
    parameters: dict[str, Any] | None,
    secrets: list[str],
    *,
    path: Path | None = None,
) -> None:
    """Walk the Jinja2 AST of ``command`` and reject every interpolation
    that would constitute a shell-injection or secret-leak vector.

    Per spec, this uses an AST walk — regex scanning of the template
    source is forbidden because Jinja2's surface (filter chains, block
    statements, conditionals, subscripts) is too rich to match safely.
    """

    env = jinja2.Environment()
    try:
        ast = env.parse(command)
    except jinja2.TemplateSyntaxError as exc:
        raise InspectorError(
            kind="command_template_invalid",
            path=path,
            line=int(exc.lineno) if exc.lineno is not None else None,
            message=str(exc.message) if exc.message is not None else "",
        ) from exc

    parents = _build_parent_map(ast)
    param_types = _param_type_lookup(parameters)
    secret_set = frozenset(secrets)

    # ---- Pass 1 — every Getitem whose const-arg matches a secret name ----
    for getitem in ast.find_all(nodes.Getitem):
        arg = getitem.arg
        if isinstance(arg, nodes.Const) and isinstance(arg.value, str) and arg.value in secret_set:
            raise InspectorError(
                kind="secret_inlined_in_command",
                path=path,
                secret=arg.value,
            )

    # ---- Pass 2 — every Name node ----
    for name_node in ast.find_all(nodes.Name):
        name = name_node.name

        # Secret in direct interpolation position.
        if name in secret_set:
            raise InspectorError(
                kind="secret_inlined_in_command",
                path=path,
                secret=name,
            )

        schema = param_types.get(name)
        if schema is None:
            # Not a declared parameter — could be a Jinja2 builtin name, a
            # loop variable, or a typo. M1 does not allow unbound names in
            # `collect.command` for shell injection reasons, but
            # enforcement of "unknown name" is out of scope here (Jinja2
            # would surface UndefinedError at render time inside the
            # runner). Skip.
            continue

        schema_type = schema.get("type")
        chain = _filter_chain(name_node, parents)

        # Determine the effective type — if the Name is the inner node of a
        # Getitem (subscript), treat as the element type for the array case.
        parent = parents.get(id(name_node))
        is_subscripted = isinstance(parent, nodes.Getitem) and parent.node is name_node

        if is_subscripted and schema_type == "array":
            # `endpoints[0]` (or `servers[0].host` / `servers[0]['host']`) —
            # element type drives validation. When the subscript is followed
            # by a further attribute / string-subscript chain, delegate to
            # the same object-member walker as for top-level object params
            # so the leaf type can be resolved through the array's `items`
            # schema. Otherwise fall back to the simple element-type check.
            assert isinstance(parent, nodes.Getitem)  # narrow for mypy
            grandparent = parents.get(id(parent))
            chain_continues = (
                isinstance(grandparent, (nodes.Getattr, nodes.Getitem))
                and grandparent.node is parent
            )
            if chain_continues:
                _validate_object_member_access(name_node, schema, name, parents, path=path)
                continue
            items_class = _classify_array_items(schema, name)
            if items_class == "undetermined":
                raise InspectorError(
                    kind="array_parameter_items_type_undetermined",
                    path=path,
                    parameter=name,
                )
            if items_class == "string":
                # Subscripted single string element — must flow through `| sh`.
                # The filter chain is computed against the Getitem node (the
                # outer wrapper), not the Name itself, because the Filter
                # parent applies to the Getitem expression.
                subscript_chain = _filter_chain_from(parent, parents)
                if not _chain_contains_sh(subscript_chain):
                    raise InspectorError(
                        kind="unquoted_parameter_in_command",
                        path=path,
                        parameter=name,
                    )
            # Numeric items + subscript → no filter required.
            continue

        if schema_type == "string":
            # For `{{ host[0] | sh }}` (subscript on a string), the filter
            # chain wraps the Getitem, not the Name itself. Use the
            # subscript-rooted chain in that case so `| sh` applied to the
            # subscript expression counts.
            effective_chain = chain
            if is_subscripted and isinstance(parent, nodes.Getitem):
                effective_chain = _filter_chain_from(parent, parents)
            if not _chain_contains_sh(effective_chain):
                raise InspectorError(
                    kind="unquoted_parameter_in_command",
                    path=path,
                    parameter=name,
                )
            continue

        if schema_type == "array":
            items_class = _classify_array_items(schema, name)
            if items_class == "undetermined":
                raise InspectorError(
                    kind="array_parameter_items_type_undetermined",
                    path=path,
                    parameter=name,
                )
            if items_class == "string" and not _chain_is_map_sh_then_join(chain):
                raise InspectorError(
                    kind="unquoted_array_parameter_in_command",
                    path=path,
                    parameter=name,
                )
            # Numeric items — no filter requirement.
            continue

        if schema_type == "object":
            # Bare object-typed Name with no member access (e.g. `{{ db }}`)
            # is not a shell-injection vector by itself — Jinja2 would
            # stringify the dict; the manifest author hits an obvious bug
            # at render time. Member-access chains (`{{ db.host }}` /
            # `{{ db['host'] }}` / `{{ db.deep.host }}`) require the leaf
            # type to flow through the same string / array-of-strings
            # gating as a top-level parameter — handled below.
            _validate_object_member_access(name_node, schema, name, parents, path=path)
            continue

        # Numeric / boolean scalar parameters — no filter requirement.


# Sentinel values returned by `_walk_member_chain` instead of `None`. They
# distinguish "no chain — bare reference / dynamic subscript on schema-typed
# object" from "chain contains an integer subscript that must be resolved by
# inspecting the parent schema at walk time" so the caller can attach the
# right error kind.
_WALK_DYNAMIC_SUBSCRIPT: object = object()
_WALK_INVALID_INT_SUBSCRIPT_ON_OBJECT: object = object()


def _walk_member_chain(
    name_node: nodes.Name, parents: dict[int, nodes.Node]
) -> tuple[list[tuple[str | int, nodes.Node]], nodes.Node] | object | None:
    """Collect a member-access chain rooted at ``name_node``.

    Returns ``(steps, outermost)`` where ``steps`` is a list of
    ``(key, ancestor_node)`` pairs — one per ``Getattr`` /
    ``Getitem(Const str)`` / ``Getitem(Const int)`` ancestor — and
    ``outermost`` is the last ancestor in the chain (the node a filter
    chain would wrap). ``key`` is the attribute name (str) for string
    accesses or an int for integer subscripts.

    Sentinel returns:

      * ``None`` — Name has no member-access ancestor (bare reference).
      * ``_WALK_DYNAMIC_SUBSCRIPT`` — chain contains a ``Getitem`` whose
        ``arg`` is neither a constant string nor a constant int (dynamic
        subscripts on schema-typed objects are unsafe in M1).

    Distinguishing the two sentinels lets the caller raise
    ``unquoted_parameter_in_command`` for dynamic subscripts without
    confusing them with bare references that don't need any validation.
    """

    steps: list[tuple[str | int, nodes.Node]] = []
    current: nodes.Node = name_node
    while True:
        parent = parents.get(id(current))
        if parent is None:
            break
        if isinstance(parent, nodes.Getattr) and parent.node is current:
            steps.append((parent.attr, parent))
            current = parent
            continue
        if isinstance(parent, nodes.Getitem) and parent.node is current:
            arg = parent.arg
            if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
                steps.append((arg.value, parent))
                current = parent
                continue
            if (
                isinstance(arg, nodes.Const)
                and isinstance(arg.value, int)
                and not isinstance(arg.value, bool)
            ):
                # Integer subscript — resolution to `array.items` or
                # `unquoted_parameter_in_command` happens in
                # `_resolve_member_leaf` against the parent schema at this
                # point in the chain.
                steps.append((arg.value, parent))
                current = parent
                continue
            # Non-const subscript on a schema-typed object — unsafe.
            return _WALK_DYNAMIC_SUBSCRIPT
        break
    if not steps:
        return None
    return steps, current


def _resolve_member_leaf(
    root_schema: dict[str, Any], member_path: list[str | int]
) -> dict[str, Any] | object | None:
    """Walk ``root_schema`` down ``member_path`` and return the leaf
    property schema, ``None`` for "no resolution along the chain", or the
    sentinel ``_WALK_INVALID_INT_SUBSCRIPT_ON_OBJECT`` when an integer
    subscript is applied to a schema whose ``type`` is not ``array``.

    String steps walk ``properties`` (requires ``type=object``); int steps
    walk ``items`` (requires ``type=array``). Any mismatch returns
    ``None`` or the integer-on-object sentinel as appropriate.
    """

    schema: dict[str, Any] = root_schema
    for step in member_path:
        if isinstance(step, int) and not isinstance(step, bool):
            # Integer subscript — parent must be array; descend into items.
            if schema.get("type") != "array":
                return _WALK_INVALID_INT_SUBSCRIPT_ON_OBJECT
            items = schema.get("items")
            if not isinstance(items, dict):
                return None
            schema = items
            continue
        # String step — parent must be object; descend into properties.
        if schema.get("type") != "object":
            return None
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return None
        next_schema = properties.get(step)
        if not isinstance(next_schema, dict):
            return None
        schema = next_schema
    return schema


def _validate_object_member_access(
    name_node: nodes.Name,
    root_schema: dict[str, Any],
    root_name: str,
    parents: dict[int, nodes.Node],
    *,
    path: Path | None,
) -> None:
    """Validate a ``{{ root.a.b }}`` / ``{{ root['a'] }}`` style chain.

    Resolves the leaf schema by walking ``root_schema.properties`` and
    applies the same string / array(string-items) / numeric rules as the
    top-level walker. The ``parameter`` field of any raised
    ``InspectorError`` carries the dot-joined path (e.g. ``db.host``).
    """

    walk = _walk_member_chain(name_node, parents)
    if walk is None:
        # Bare object reference (no member access — not a shell vector).
        return
    if walk is _WALK_DYNAMIC_SUBSCRIPT:
        # Dynamic subscript like ``{{ db[user_input] }}`` — unsafe in M1
        # because the leaf type cannot be resolved statically.
        raise InspectorError(
            kind="unquoted_parameter_in_command",
            path=path,
            parameter=root_name,
        )

    assert isinstance(walk, tuple)  # narrow for mypy
    steps, outermost = walk
    member_path: list[str | int] = [attr for attr, _node in steps]
    dot_path = ".".join([root_name, *(str(s) for s in member_path)])

    leaf_schema = _resolve_member_leaf(root_schema, member_path)
    if leaf_schema is _WALK_INVALID_INT_SUBSCRIPT_ON_OBJECT:
        # Integer subscript applied to a non-array (object / scalar) parent
        # is nonsensical and likely malicious — Jinja2 stringifies the
        # result as `None`, which would silently drop the parameter from
        # the command. Reject statically.
        raise InspectorError(
            kind="unquoted_parameter_in_command",
            path=path,
            parameter=dot_path,
        )
    if leaf_schema is None:
        # Schema lookup failed somewhere along the chain — either the
        # property is undeclared or an intermediate field is not an object.
        # Reject conservatively per spec: an unresolved leaf type cannot be
        # gated, so the manifest must declare the path before it's used.
        raise InspectorError(
            kind="unquoted_parameter_in_command",
            path=path,
            parameter=dot_path,
        )

    assert isinstance(leaf_schema, dict)  # narrow for mypy
    leaf_type = leaf_schema.get("type")
    chain = _filter_chain_from(outermost, parents)

    if leaf_type == "string":
        if not _chain_contains_sh(chain):
            raise InspectorError(
                kind="unquoted_parameter_in_command",
                path=path,
                parameter=dot_path,
            )
        return

    if leaf_type == "array":
        items_class = _classify_array_items(leaf_schema, dot_path)
        if items_class == "undetermined":
            raise InspectorError(
                kind="array_parameter_items_type_undetermined",
                path=path,
                parameter=dot_path,
            )
        if items_class == "string" and not _chain_is_map_sh_then_join(chain):
            raise InspectorError(
                kind="unquoted_array_parameter_in_command",
                path=path,
                parameter=dot_path,
            )
        return

    if leaf_type == "object":
        # The Name itself binds an object, but the access chain stopped at
        # another object — Jinja2 will stringify a dict at render time
        # which is a manifest authoring bug (not a shell-injection vector
        # because the leaf isn't user-controlled string content). Skip.
        return

    # Numeric / boolean / unknown leaf type — no filter requirement.


def _filter_chain_from(
    start: nodes.Node,
    parents: dict[int, nodes.Node],
) -> list[nodes.Filter]:
    """Like `_filter_chain` but starts from an arbitrary node (used for the
    Getitem-subscript case where the filter wraps the Getitem, not the
    inner Name).
    """

    chain: list[nodes.Filter] = []
    current: nodes.Node = start
    while True:
        parent = parents.get(id(current))
        if parent is None or not isinstance(parent, nodes.Filter):
            break
        if parent.node is not current:
            break
        chain.append(parent)
        current = parent
    return chain


# --------------------------------------------------------------------------- #
# `_validate_findings`
# --------------------------------------------------------------------------- #


_AGGREGATE_VAR_ATTR_PATTERN = re.compile(r"\{([a-z_][a-z_0-9]*)\.")


def _validate_findings(findings: list[FindingRule]) -> None:
    """Re-check the aggregate-mode `{var.attr}` reference guard at manifest
    level so that error surfaces include the rule's `index` field.

    `FindingRule.model_validator` already raises a Pydantic `ValueError`
    for this case (which loaders wrap into `manifest_validation_error`),
    but that path loses the rule index. This loader-level pass surfaces
    the same misuse as `InspectorError(kind="finding_message_invalid_aggregate_ref",
    index=i, var=...)` so the doctor / CLI / log output can pinpoint
    which rule in the list is broken.
    """

    for i, rule in enumerate(findings):
        if rule.for_each is not None:
            continue
        matches = _AGGREGATE_VAR_ATTR_PATTERN.findall(rule.message)
        if matches:
            raise InspectorError(
                kind="finding_message_invalid_aggregate_ref",
                index=i,
                var=matches[0],
            )
