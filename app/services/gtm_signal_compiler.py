"""Generalized GTM signal criteria compiler (hq-x, pure Python — no DuckDB).

Compiles a signal's criteria spec into a constrained, parameterized SQL
fragment that DEX (``/api/internal/signals/compute``) assembles into the final
SELECT and executes over the Polaris Lance warehouse. The compiler lives in
hq-x (single source of truth for "what is a signal"); DEX stays a constrained
executor that only ever runs the shape it assembles.

Injection-safe by construction:
  * VALUES are never interpolated — they become positional ``?`` bindings.
  * IDENTIFIERS (columns / join keys / order_by / select) are validated against
    the live-schema allowlist (passed by the caller after a get_polaris_schema
    round-trip) AND a strict regex, then double-quoted in the WHERE fragment.
  * ``spine_target`` / join ``dataset`` are validated against a dotted-id regex.
DEX re-validates identifiers against the freshly-opened Lance schema at execute
time (defense-in-depth).

Criteria spec::

    {
      "spine_target": "<namespace>.<dataset>",
      "predicates": [ {"column": str, "op": <op>, "value": ...} ],
      "time_window": {"column": str, "hours": int} | null,
      "join": {"dataset": str, "on": [spine_col, join_col], "select": [str]} | null,
      "select": [str] | null,
      "order_by": {"column": str, "dir": "asc"|"desc"} | null,
      "limit": int | null
    }

Ops: ``eq, in, gte, lte, between, is_null, not_null, like``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_ORDER_DIRS = {"asc", "desc"}


class CompileError(ValueError):
    """Raised on an invalid or unsafe criteria spec."""


@dataclass(frozen=True)
class CompiledCriteria:
    spine_target: str
    where_sql: str            # parameterized fragment; '?' placeholders; identifiers quoted
    bindings: list[Any]
    select: list[str]         # validated raw identifiers ([] => all columns; DEX quotes)
    join: dict | None         # {"dataset","on":[l,r],"select":[...]} validated raw
    order_by: dict | None     # {"column","dir"} validated raw
    limit: int | None
    scan_filter: dict | None  # {"column","gte","lte"} BTREE-pushdown hint from time_window


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_ident(name: Any, allowed: set[str], *, kind: str = "column") -> str:
    """Validate an identifier against the schema allowlist + strict regex.

    Returns the raw (unquoted) name. Used for select/join/order_by, which DEX
    quotes itself when it assembles the SELECT.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise CompileError(f"invalid {kind} identifier: {name!r}")
    if name not in allowed:
        raise CompileError(f"unknown {kind}: {name!r} (not in dataset schema)")
    return name


def _quote_ident(name: Any, allowed: set[str], *, kind: str = "column") -> str:
    """Validate then double-quote, for interpolation into the WHERE fragment."""
    safe = _validate_ident(name, allowed, kind=kind)
    return '"' + safe.replace('"', '""') + '"'


def _validate_dotted(target: Any, *, kind: str) -> str:
    if not isinstance(target, str) or not _DOTTED_RE.match(target):
        raise CompileError(
            f"invalid {kind} (expected <namespace>.<dataset>): {target!r}"
        )
    return target


def compile_criteria(
    criteria: dict[str, Any],
    *,
    now: datetime,
    allowed_columns: set[str],
    allowed_join_columns: set[str] | None = None,
) -> CompiledCriteria:
    """Compile a criteria spec into a ``CompiledCriteria``. Raises ``CompileError``
    on any invalid op, unknown/invalid identifier, or malformed shape.
    """
    if not isinstance(criteria, dict):
        raise CompileError("criteria must be an object")

    spine_target = _validate_dotted(
        criteria.get("spine_target", ""), kind="spine_target"
    )

    clauses: list[str] = []
    bindings: list[Any] = []
    scan_filter: dict | None = None

    # ── time_window → "col" >= ? AND "col" <= ? (date-granular; legacy semantics)
    tw = criteria.get("time_window")
    if tw is not None:
        if not isinstance(tw, dict) or "column" not in tw or "hours" not in tw:
            raise CompileError("time_window must be {column, hours}")
        col_raw = _validate_ident(tw["column"], allowed_columns, kind="time_window column")
        col = '"' + col_raw.replace('"', '""') + '"'
        try:
            hours = int(tw["hours"])
        except (TypeError, ValueError):
            raise CompileError(f"time_window.hours must be int: {tw['hours']!r}")
        lo = (now - timedelta(hours=hours)).date().isoformat()
        hi = now.date().isoformat()
        clauses.append(f"{col} >= ? AND {col} <= ?")
        bindings.extend([lo, hi])
        scan_filter = {"column": col_raw, "gte": lo, "lte": hi}

    # ── predicates
    predicates = criteria.get("predicates") or []
    if not isinstance(predicates, list):
        raise CompileError("predicates must be a list")
    for p in predicates:
        if not isinstance(p, dict) or "column" not in p or "op" not in p:
            raise CompileError(f"predicate must be {{column, op, value?}}: {p!r}")
        op = p["op"]
        col = _quote_ident(p["column"], allowed_columns)

        if op == "is_null":
            clauses.append(f"{col} IS NULL")
        elif op == "not_null":
            clauses.append(f"{col} IS NOT NULL")
        elif op == "eq":
            clauses.append(f"{col} = ?")
            bindings.append(p.get("value"))
        elif op == "like":
            clauses.append(f"{col} LIKE ?")
            bindings.append(p.get("value"))
        elif op in ("gte", "lte"):
            value = p.get("value")
            lhs = f"TRY_CAST({col} AS DOUBLE)" if _is_number(value) else col
            sym = ">=" if op == "gte" else "<="
            clauses.append(f"{lhs} {sym} ?")
            bindings.append(value)
        elif op == "between":
            value = p.get("value")
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise CompileError(f"between requires [lo, hi]: {value!r}")
            lo_v, hi_v = value[0], value[1]
            numeric = _is_number(lo_v) and _is_number(hi_v)
            lhs = f"TRY_CAST({col} AS DOUBLE)" if numeric else col
            clauses.append(f"({lhs} >= ? AND {lhs} <= ?)")
            bindings.extend([lo_v, hi_v])
        elif op == "in":
            value = p.get("value")
            if not isinstance(value, (list, tuple)) or not value:
                raise CompileError(f"in requires a non-empty list: {value!r}")
            non_null = [v for v in value if v is not None]
            include_null = any(v is None for v in value)
            sub: list[str] = []
            if non_null:
                placeholders = ",".join(["?"] * len(non_null))
                sub.append(f"{col} IN ({placeholders})")
                bindings.extend(non_null)
            if include_null:
                sub.append(f"{col} IS NULL")
            clauses.append("(" + " OR ".join(sub) + ")")
        else:
            raise CompileError(f"unsupported op: {op!r}")

    where_sql = " AND ".join(clauses) if clauses else "TRUE"

    # ── select (validated raw; DEX quotes)
    select_raw = criteria.get("select") or []
    if not isinstance(select_raw, list):
        raise CompileError("select must be a list")
    select = [_validate_ident(c, allowed_columns, kind="select column") for c in select_raw]

    # ── join
    join_spec = criteria.get("join")
    join: dict | None = None
    if join_spec is not None:
        if not isinstance(join_spec, dict):
            raise CompileError("join must be an object")
        if allowed_join_columns is None:
            raise CompileError("join specified but allowed_join_columns not provided")
        jdataset = _validate_dotted(join_spec.get("dataset", ""), kind="join dataset")
        on = join_spec.get("on")
        if not isinstance(on, (list, tuple)) or len(on) != 2:
            raise CompileError("join.on must be [spine_col, join_col]")
        left = _validate_ident(on[0], allowed_columns, kind="join left column")
        right = _validate_ident(on[1], allowed_join_columns, kind="join right column")
        jselect = [
            _validate_ident(c, allowed_join_columns, kind="join select column")
            for c in (join_spec.get("select") or [])
        ]
        join = {"dataset": jdataset, "on": [left, right], "select": jselect}

    # ── order_by
    ob_spec = criteria.get("order_by")
    order_by: dict | None = None
    if ob_spec is not None:
        if not isinstance(ob_spec, dict) or "column" not in ob_spec:
            raise CompileError("order_by must be {column, dir?}")
        ob_col = _validate_ident(ob_spec["column"], allowed_columns, kind="order_by column")
        ob_dir = (ob_spec.get("dir") or "desc").lower()
        if ob_dir not in _ORDER_DIRS:
            raise CompileError(f"order_by.dir must be asc|desc: {ob_dir!r}")
        order_by = {"column": ob_col, "dir": ob_dir}

    # ── limit
    limit = criteria.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise CompileError(f"limit must be int: {limit!r}")

    return CompiledCriteria(
        spine_target=spine_target,
        where_sql=where_sql,
        bindings=bindings,
        select=select,
        join=join,
        order_by=order_by,
        limit=limit,
        scan_filter=scan_filter,
    )


__all__ = ["CompileError", "CompiledCriteria", "compile_criteria"]
