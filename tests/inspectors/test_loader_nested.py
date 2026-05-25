"""Tests for nested-object parameter access in `_validate_command_template`.

When `parameters.<root>` is declared as `type: object` and the template
performs member access (`{{ root.leaf }}` / `{{ root['leaf'] }}` /
multi-level `{{ root.a.b }}`), the leaf type drives the same gating
rules as a top-level parameter:

  * string leaf → must flow through `| sh`
  * array(string-items) leaf → must flow through `| map('sh') | join(...)`
  * array(undetermined-items) leaf → raise `array_parameter_items_type_undetermined`
  * undeclared leaf along the chain → raise `unquoted_parameter_in_command`
  * dynamic subscript (`{{ root[name_var] }}`) → raise

Numeric/boolean leaves carry no filter requirement (same as top-level).
"""

from __future__ import annotations

import pytest

from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.loader import _validate_command_template

# --------------------------------------------------------------------------- #
# Schema fixtures
# --------------------------------------------------------------------------- #

# `{ db: { host: string, port: integer, tags: array(string), deep: { host: string } } }`
_DB_NESTED = {
    "properties": {
        "db": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "pattern": r"^[a-zA-Z0-9.-]+$"},
                "port": {"type": "integer"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "pattern": r"^[a-z0-9-]+$"},
                },
                "deep": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "pattern": r"^[a-zA-Z0-9.-]+$"},
                    },
                },
                "untyped_array": {
                    # `items` missing — undetermined leaf.
                    "type": "array",
                },
            },
        },
    }
}


class TestNestedObjectStringLeaf:
    def test_nested_string_without_sh_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("psql -h {{ db.host }}", _DB_NESTED, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db.host"

    def test_nested_string_with_sh_ok(self) -> None:
        _validate_command_template("psql -h {{ db.host | sh }}", _DB_NESTED, [])

    def test_three_level_nested_string_without_sh_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("psql -h {{ db.deep.host }}", _DB_NESTED, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db.deep.host"

    def test_three_level_nested_string_with_sh_ok(self) -> None:
        _validate_command_template(
            "psql -h {{ db.deep.host | sh }}", _DB_NESTED, []
        )

    def test_const_subscript_string_without_sh_raises(self) -> None:
        # `{{ db['host'] }}` is equivalent to `{{ db.host }}` — both must
        # gate the leaf string through `| sh`.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("psql -h {{ db['host'] }}", _DB_NESTED, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db.host"

    def test_const_subscript_string_with_sh_ok(self) -> None:
        _validate_command_template(
            "psql -h {{ db['host'] | sh }}", _DB_NESTED, []
        )


class TestNestedObjectNumericLeaf:
    def test_nested_integer_without_sh_ok(self) -> None:
        # Numeric leaves don't constitute a shell-injection vector.
        _validate_command_template("psql -p {{ db.port }}", _DB_NESTED, [])


class TestNestedObjectArrayLeaf:
    def test_nested_array_with_map_sh_join_ok(self) -> None:
        _validate_command_template(
            "tag={{ db.tags | map('sh') | join(',') }}", _DB_NESTED, []
        )

    def test_nested_array_without_map_sh_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "tag={{ db.tags | join(',') }}", _DB_NESTED, []
            )
        assert exc.value.kind == "unquoted_array_parameter_in_command"
        assert exc.value.parameter == "db.tags"

    def test_nested_untyped_array_raises(self) -> None:
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "x={{ db.untyped_array | join(',') }}", _DB_NESTED, []
            )
        assert exc.value.kind == "array_parameter_items_type_undetermined"
        assert exc.value.parameter == "db.untyped_array"


class TestNestedObjectInvalidAccess:
    def test_undeclared_leaf_raises(self) -> None:
        # `{{ db.nonexistent }}` — the parameter `db` is declared but
        # `db.properties.nonexistent` is not. The chain cannot be statically
        # resolved so the loader must reject (per spec: schema lookup
        # failure at any level → raise).
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "psql -h {{ db.nonexistent }}", _DB_NESTED, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db.nonexistent"

    def test_dynamic_subscript_raises(self) -> None:
        # `{{ db[user_input] }}` — subscript whose key is itself a Name
        # (or any non-Const) cannot be resolved statically. Reject in M1.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "psql -h {{ db[user_input] }}", _DB_NESTED, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db"

    def test_bare_object_reference_does_not_raise(self) -> None:
        # `{{ db }}` with no member access — Jinja2 would stringify the
        # dict and the manifest author hits a render-time bug, but it's
        # not a shell-injection vector by itself. Loader must not reject.
        _validate_command_template("echo {{ db }}", _DB_NESTED, [])


# --------------------------------------------------------------------------- #
# Integer subscript handling — round-2 fix 8.
#
# Numeric `Getitem` (e.g. `[0]`) in a member chain previously bypassed
# validation: `_walk_member_chain` returned None and the leaf check silently
# passed. The fix resolves int subscripts against the parent schema:
# array-typed parent descends into `items`; object-typed parent raises.
# --------------------------------------------------------------------------- #


# `{ servers: array of { host: string, port: integer } }`
_SERVERS_ARRAY = {
    "properties": {
        "servers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "pattern": r"^[a-zA-Z0-9.-]+$",
                    },
                    "port": {"type": "integer"},
                },
            },
        },
    }
}


class TestIntegerSubscriptOnArray:
    def test_int_subscript_into_object_items_string_leaf_with_sh_ok(self) -> None:
        # `{{ servers[0].host | sh }}` — array → object items → string leaf
        # protected by `| sh`. Should load.
        _validate_command_template(
            "ping {{ servers[0].host | sh }}", _SERVERS_ARRAY, []
        )

    def test_int_subscript_into_object_items_string_leaf_without_sh_raises(
        self,
    ) -> None:
        # `{{ servers[0].host }}` — string leaf must flow through `| sh`.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ servers[0].host }}", _SERVERS_ARRAY, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "servers.0.host"

    def test_int_subscript_into_object_items_numeric_leaf_ok(self) -> None:
        # Numeric leaf doesn't require `| sh`.
        _validate_command_template(
            "ping {{ servers[0].port }}", _SERVERS_ARRAY, []
        )

    def test_int_subscript_then_const_string_subscript_with_sh_ok(self) -> None:
        # `{{ servers[0]['host'] | sh }}` — mixed int + str subscripts.
        _validate_command_template(
            "ping {{ servers[0]['host'] | sh }}", _SERVERS_ARRAY, []
        )


class TestIntegerSubscriptOnObject:
    def test_int_subscript_on_object_raises(self) -> None:
        # `{{ db[0] }}` — integer subscript on a `type: object` parameter
        # is nonsensical and previously slipped through the validator.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("ping {{ db[0] }}", _DB_NESTED, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db.0"

    def test_int_subscript_on_object_with_further_chain_raises(self) -> None:
        # `{{ db[0].host }}` — same scenario but with a further chain. The
        # integer subscript on the object root still raises before reaching
        # the further chain.
        with pytest.raises(InspectorError) as exc:
            _validate_command_template(
                "ping {{ db[0].host | sh }}", _DB_NESTED, []
            )
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "db.0.host"
