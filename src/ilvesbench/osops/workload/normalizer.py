from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any

try:
    from pglast import ast, parse_sql
    from pglast.stream import RawStream
    from pglast.visitors import Skip, Visitor
except Exception:  # pragma: no cover - exercised only when dependency is missing
    ast = None
    parse_sql = None
    RawStream = None
    Skip = None
    Visitor = object


@dataclass(slots=True)
class SQLParameter:
    position: int
    inferred_type: str
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SQLNormalizationResult:
    raw_sql: str
    normalized_sql: str
    fingerprint: str
    parameters: list[SQLParameter] = field(default_factory=list)


class SQLNormalizationError(ValueError):
    pass


class PglastSQLNormalizer:
    """PostgreSQL-aware SQL template normalizer backed by pglast/libpg_query."""

    def __init__(self, *, parameterize_limits: bool = False) -> None:
        self._parameterize_limits = parameterize_limits

    def normalize(self, sql: str) -> SQLNormalizationResult:
        if parse_sql is None or RawStream is None or ast is None:
            raise SQLNormalizationError("pglast is required for PostgreSQL-aware SQL normalization.")
        raw_sql = sql.strip().rstrip(";")
        if not raw_sql:
            raise SQLNormalizationError("SQL statement is empty.")
        try:
            tree = parse_sql(raw_sql)
        except Exception as exc:
            raise SQLNormalizationError(f"PostgreSQL parser rejected statement: {exc}") from exc
        if len(tree) != 1:
            raise SQLNormalizationError("Expected one SQL statement per log observation.")

        parameterizer = _ConstantParameterizer(parameterize_limits=self._parameterize_limits)
        parameterizer(tree)
        normalized_sql = RawStream()(tree).strip().rstrip(";") + ";"
        fingerprint = hashlib.sha256(normalized_sql.encode("utf-8")).hexdigest()
        return SQLNormalizationResult(
            raw_sql=raw_sql + ";",
            normalized_sql=normalized_sql,
            fingerprint=fingerprint,
            parameters=parameterizer.parameters,
        )


class _ConstantParameterizer(Visitor):
    def __init__(self, *, parameterize_limits: bool) -> None:
        super().__init__()
        self._parameterize_limits = parameterize_limits
        self._next_param = 1
        self.parameters: list[SQLParameter] = []

    def visit_A_Const(self, ancestors, node):  # noqa: N802 - pglast visitor naming
        if not self._parameterize_limits and _ancestor_mentions(ancestors, "limitCount"):
            return Skip
        param_number = self._next_param
        self._next_param += 1
        inferred_type = self._literal_type(node)
        example = self._literal_example(node)
        self.parameters.append(
            SQLParameter(
                position=param_number,
                inferred_type=inferred_type,
                examples=[example] if example else [],
            )
        )
        return ast.ParamRef(number=param_number)

    def _literal_type(self, node) -> str:
        if bool(getattr(node, "isnull", False)):
            return "null"
        value = getattr(node, "val", None)
        class_name = value.__class__.__name__.lower() if value is not None else "unknown"
        if "integer" in class_name:
            return "integer"
        if "float" in class_name:
            return "float"
        if "string" in class_name:
            return _string_literal_type(getattr(value, "sval", ""))
        if "boolean" in class_name:
            return "boolean"
        return class_name or "unknown"

    def _literal_example(self, node) -> str:
        if bool(getattr(node, "isnull", False)):
            return "NULL"
        value = getattr(node, "val", None)
        for attr in ("ival", "fval", "sval", "boolval"):
            if hasattr(value, attr):
                return str(getattr(value, attr))
        return ""


def _ancestor_mentions(ancestors: Any, token: str) -> bool:
    return token in str(ancestors)


def _string_literal_type(value: str) -> str:
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?$", value):
        return "datetime"
    if value.strip().startswith(("{", "[")):
        return "json_or_array"
    return "string"
