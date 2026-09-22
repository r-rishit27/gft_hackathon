"""Model-side schema screening; the execution service still enforces policy."""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify


def normalize_date_trunc(sql):
    """Translate only SQLCoder's known Postgres argument order, never guess names."""
    try:
        statements = sqlglot.parse(sql, read="bigquery")
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
            return sql, []
        tree = statements[0]
        postgres_dates = {}
        try:
            alternate = sqlglot.parse_one(sql, read="postgres")
            postgres_dates = {n.meta.get("start"): n for n in alternate.find_all(exp.TimestampTrunc)}
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            pass
        corrections = []
        for node in tree.find_all(exp.DateTrunc):
            first, second = node.this, node.args.get("unit")
            # GoogleSQL parses an unqualified date-part argument as a literal.
            # Recover its original expression using the Postgres AST, not by
            # guessing whether a string happens to look like a column name.
            original = postgres_dates.get(node.meta.get("start"))
            if isinstance(second, exp.Literal) and original is not None:
                second = original.this
            if isinstance(first, exp.Literal) and first.is_string and first.this.upper() in {
                "DAY", "WEEK", "MONTH", "QUARTER", "YEAR", "HOUR", "MINUTE", "SECOND"
            } and isinstance(second, (exp.Column, exp.Cast, exp.Date, exp.Timestamp)):
                before = node.sql(dialect="bigquery")
                node.set("this", second.copy())
                node.set("unit", exp.Var(this=first.this.upper()))
                corrections.append({"from": before, "to": node.sql(dialect="bigquery"),
                                    "kind": "dialect", "stage": "google_sql_normalization"})
        return (tree.sql(dialect="bigquery") if corrections else sql), corrections
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        return sql, []


def validate_candidate(sql, tables):
    if "CLARIFICATION_REQUIRED" in sql:
        return ["clarification_required"]
    try:
        parsed = sqlglot.parse(sql, read="bigquery")
        if len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.Union)):
            return ["A single GoogleSQL SELECT query is required"]
        tree = parsed[0]
        for table in tree.find_all(exp.Table):
            if table.catalog or table.db:
                if table.catalog not in ("", "gen-lang-client-0810987953") or table.db != "aml_demo":
                    return ["Unknown project or dataset"]
                table.set("catalog", None)
                table.set("db", None)
                table.meta.clear()
        schema = {name.lower(): {f["name"]: f["sql_type"] for f in t["fields"]} for name, t in tables.items()}
        qualify(tree, dialect="bigquery", schema=schema, infer_schema=False, validate_qualify_columns=True)
        return []
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        return ["Generated SQL does not resolve against the approved GoogleSQL schema"]
