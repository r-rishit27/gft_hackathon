import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope as QueryScope, traverse_scope

from .config import DATASET, PROJECT, Scope
from .errors import AnalyticsError

ALLOWED_FUNCTIONS = {
    "COUNT", "COUNT_IF", "SUM", "AVG", "MIN", "MAX", "COALESCE", "IF", "CAST",
    "TRY_CAST", "DATE", "TIMESTAMP", "DATE_TRUNC", "TIMESTAMP_TRUNC", "TIMESTAMP_DIFF",
    "DATE_DIFF", "SAFE_DIVIDE", "ROW_NUMBER", "RANK", "DENSE_RANK", "ROUND", "ABS",
    "EXTRACT", "NULLIF", "LOWER", "UPPER", "AND", "OR", "NOT",
    "CASE", "CONCAT", "CONCAT_WS", "SUBSTRING", "SPLIT", "STARTS_WITH", "ENDS_WITH",
    "REGEXP_EXTRACT", "REGEXP_LIKE", "LENGTH", "ARRAY_SIZE", "ARRAY_AGG", "STRUCT",
    "APPROX_QUANTILE", "APPROX_DISTINCT", "LEAD", "LAG", "FIRST_VALUE", "LAST_VALUE",
    "FLOOR", "CEIL", "LOG", "SQRT", "POWER", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
    "VARIANCE", "VARIANCE_POP", "PERCENTILE_CONT", "PERCENTILE_DISC", "TRIM",
    "TIMESTAMP_ADD", "TIMESTAMP_SUB", "DATE_ADD", "DATE_SUB", "CURRENT_DATE", "CURRENT_TIMESTAMP",
    "TIME_TO_STR", "STR_TO_DATE", "STR_TO_TIME", "DATE_FROM_PARTS", "UNNEST",
}
FORBIDDEN_NODES = {
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Merge", "Command", "Copy",
    "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Execute", "Into", "Lock",
    "SessionParameter", "Parameter", "Placeholder", "Pivot", "Unpivot", "TableSample",
}


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    sha256: str
    warnings: tuple[str, ...] = ()


class SQLValidator:
    def __init__(self, catalog: dict):
        self.catalog = catalog

    def _parse(self, sql: str):
        if len(sql) > 32_000:
            raise AnalyticsError("sql_rejected", "Query exceeds the size limit.")
        try:
            statements = sqlglot.parse(sql, read="bigquery")
        except (SqlglotError, RecursionError):
            raise AnalyticsError("sql_rejected", "SQL could not be parsed as GoogleSQL.") from None
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
            raise AnalyticsError("sql_rejected", "Exactly one read-only SELECT query is permitted.")
        tree = statements[0]
        nodes = list(tree.walk())
        if len(nodes) > 2000:
            raise AnalyticsError("sql_rejected", "Query is too complex.")
        for node in nodes:
            if type(node).__name__ in FORBIDDEN_NODES:
                raise AnalyticsError("sql_rejected", "Query contains an unsupported operation.")
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise AnalyticsError("sql_rejected", "Recursive queries are not enabled.")
            if isinstance(node, exp.Func) and node.sql_name() not in ALLOWED_FUNCTIONS:
                raise AnalyticsError("sql_rejected", "Query uses an unapproved function.")
            if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
                raise AnalyticsError("sql_rejected", "Qualified routine calls are not permitted.")
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier) or any(
                table.args.get(k) for k in ("version", "pivots", "sample", "changes")
            ):
                raise AnalyticsError("sql_rejected", "Only ordinary approved tables are permitted.")
        return tree

    def validate(self, sql: str, scope: Scope) -> ValidatedQuery:
        tree = self._parse(sql)
        resources = scope.resources
        schema = {}
        for name, resource in resources.items():
            table = self.catalog["tables"].get(name)
            if not table:
                raise AnalyticsError("configuration", "An approved resource is not in the catalog.", 503)
            available = {f["name"]: f["sql_type"] for f in table["fields"]}
            if not set(resource.columns) <= set(available):
                raise AnalyticsError("configuration", "Approved columns do not match the catalog.", 503)
            schema[name.lower()] = {c: available[c] for c in resource.columns}

        # Resolve physical sources via scopes, not by collecting CTE names globally.
        physical = []
        try:
            for query_scope in traverse_scope(tree):
                for _, source in query_scope.selected_sources.values():
                    if isinstance(source, QueryScope):
                        continue
                    if not isinstance(source, exp.Table):
                        raise AnalyticsError("sql_rejected", "Unsupported table source.")
                    name = source.name
                    if name not in resources:
                        raise AnalyticsError("resource_denied", "Query references a resource outside your approved scope.", 403)
                    if (source.catalog and source.catalog != PROJECT) or (source.db and source.db != DATASET):
                        raise AnalyticsError("resource_denied", "Query references an unapproved project or dataset.", 403)
                    if source.catalog and not source.db:
                        raise AnalyticsError("resource_denied", "Incomplete resource path.", 403)
                    source.set("catalog", None)
                    source.set("db", None)
                    physical.append(source)
            if not physical:
                raise AnalyticsError("sql_rejected", "A query must use an approved data resource.")
            tree = qualify(tree, dialect="bigquery", schema=schema, infer_schema=False,
                           validate_qualify_columns=True, quote_identifiers=False)
            # Resolve three-part STRUCT columns before renaming table aliases;
            # SQLGlot otherwise mistakes t.amount.units for a resource path.
            tree = qualify(tree, dialect="bigquery", schema=schema, infer_schema=False,
                           validate_qualify_columns=True, quote_identifiers=False,
                           canonicalize_table_aliases=True)
        except (SqlglotError, RecursionError, ValueError):
            raise AnalyticsError("sql_rejected", "Columns or query scopes could not be validated.") from None

        for dot in tree.find_all(exp.Dot):
            parent_type = dot.this.type
            if not parent_type or parent_type.this != exp.DataType.Type.STRUCT:
                raise AnalyticsError("sql_rejected", "Nested field type could not be validated.")
            fields = {field.name.lower() for field in parent_type.expressions}
            if not isinstance(dot.expression, exp.Identifier) or dot.expression.name.lower() not in fields:
                raise AnalyticsError("sql_rejected", "Unknown nested field.")
        if any(source.name.lower() == "party" for source in physical):
            aggregates = list(tree.find_all(exp.AggFunc))
            sensitive = [a for a in aggregates if not isinstance(a, (exp.Min, exp.Max, exp.RowNumber, exp.Rank, exp.DenseRank))]
            unique_ids_only = (sensitive and all(isinstance(a, exp.Count) and isinstance(a.this, exp.Distinct) for a in sensitive)
                               and all(c.name.lower() == "party_id" for c in tree.find_all(exp.Column)))
            if sensitive and not unique_ids_only and not list(tree.find_all(exp.RowNumber)):
                raise AnalyticsError("historical_ambiguity", "Party has historical versions. Ask for the latest record per customer before aggregating; this query was not executed.")
        for query_scope in traverse_scope(tree):
            for _, source in query_scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    by_normalized_name = {k.lower(): v for k, v in resources.items()}
                    project, dataset, view = by_normalized_name[source.name.lower()].view.split(".")
                    source.set("catalog", exp.to_identifier(project, quoted=True))
                    source.set("db", exp.to_identifier(dataset, quoted=True))
                    source.set("this", exp.to_identifier(view, quoted=True))
                    source.meta.clear()
                    source.meta["quoted_table"] = True
        sql = tree.sql(dialect="bigquery", comments=False)
        warnings = []
        if any(t.name == "Party" for t in tree.find_all(exp.Table)) and list(tree.find_all(exp.Join)):
            if not list(tree.find_all(exp.RowNumber)):
                warnings.append("Party contains historical versions. Review the join's as-of condition to avoid duplicated customers.")
        return ValidatedQuery(sql, hashlib.sha256(sql.encode()).hexdigest(), tuple(warnings))

    def validate_metric(self, candidate: str, expected: str, scope: Scope) -> ValidatedQuery:
        actual = self.validate(candidate, scope)
        gold = self.validate(expected, scope)
        if actual.sql != gold.sql:
            raise AnalyticsError(
                "semantic_mismatch",
                "The generated query differs from the reviewed KPI definition; nothing was executed.",
            )
        return actual
