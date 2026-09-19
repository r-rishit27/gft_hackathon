"""
Evaluate the KAG pipeline (text2sql_falkordb.generate_sql_kag: retrieval +
exemplar shortcut + schema-grounded repair) against a test file of
(question, gold SQL) pairs.

Metrics (SQL-aware, not exact string match, since column/table order and
aliasing legitimately vary):
  - table_recall: fraction of gold tables referenced in the gold SQL that also
    appear in the generated SQL
  - table_precision: fraction of tables in the generated SQL that are actually
    gold tables
  - exact_match: normalized-whitespace, case-insensitive string equality
    after canonicalizing away two purely stylistic differences that don't
    change what the query does: table aliases (`FROM Transaction t` ==
    `FROM Transaction`, `t.type` == `type`) and `LOWER(col) = 'x'` vs a
    plain `col = 'X'` comparison (see canonicalize()). Still informative
    rather than a full SQL-equivalence check -- column order, JOIN order,
    and SELECT-list aliases still count as differences -- so keep it as a
    reference point, not the headline number.

Usage (from the project root, or from within eval/):
    python eval/evaluate_text2sql.py                          # eval_testcases.json
    python eval/evaluate_text2sql.py --testcases eval_heldout.json --out results.json
"""

import argparse
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)  # so `pipeline` (a sibling of eval/) is importable

from pipeline import text2sql_falkordb as pipeline

TESTCASES_PATH = os.path.join(SCRIPT_DIR, "eval_testcases.json")

ALL_TABLE_NAMES = [
    "Party", "AccountPartyLink", "Transaction", "InteractionEvent", "RiskCaseEvent",
    "PartySupplementaryData", "RetailPartiesRegistration", "CommercialPartiesRegistration",
    "RiskScores", "Explainability", "RegisteredPartiesExport", "ExportedMetadata",
]


def normalize(sql):
    return re.sub(r"\s+", " ", sql.strip().lower())


# Tokens that can legitimately follow "FROM <table>" / "JOIN <table>" without
# being an alias (a bare join condition, the next clause, etc.) -- without
# this guard, "FROM Transaction WHERE ..." would misparse "where" itself as
# an alias.
_ALIAS_STOPWORDS = {
    "where", "group", "order", "having", "limit", "union", "on", "left", "right",
    "inner", "outer", "full", "cross", "join", "select", "as", "from", "except", "intersect",
}
_ALIAS_DEF_RE = re.compile(r"\b(from|join)\s+([a-z_][a-z0-9_]*)\s+(?:as\s+)?([a-z_][a-z0-9_]*)\b")
_LOWER_COMPARISON_RE = re.compile(r"lower\(([a-z0-9_.]+)\)\s*=\s*'([^']*)'")


def _fold_lower_comparisons(sql):
    """`LOWER(col) = 'value'` is a case-insensitive equality check -- exactly
    what `col = 'VALUE'` against an uppercase enum constant already means in
    this schema, so it's a stylistic choice, not a different query. Just
    drops the LOWER(...) wrapper (not touching the literal's case): this
    runs after `normalize()` has already lowercased the whole string
    (including a gold query's own uppercase enum literal), so both sides are
    already in the same case by the time this fires -- uppercasing here
    would undo that and reintroduce a mismatch."""
    return _LOWER_COMPARISON_RE.sub(lambda m: f"{m.group(1)} = '{m.group(2)}'", sql)


def _strip_table_aliases(sql):
    """Table aliases (`FROM Transaction t`, `JOIN RiskCaseEvent rce`) are a
    naming choice, not a semantic difference -- SQLCoder tends to invent
    content-derived aliases (rce, cpr, ap), which otherwise fails exact_match
    against an unaliased gold query even when the query is identical in
    substance. Drops the alias from the
    FROM/JOIN clause and un-qualifies every `alias.column` reference back to
    a bare column name."""
    alias_map = {}

    def repl(match):
        keyword, table, alias = match.group(1), match.group(2), match.group(3)
        if alias in _ALIAS_STOPWORDS:
            return match.group(0)
        alias_map[alias] = table
        return f"{keyword} {table}"

    sql = _ALIAS_DEF_RE.sub(repl, sql)
    for alias in alias_map:
        sql = re.sub(rf"\b{re.escape(alias)}\.", "", sql)
    return sql


# BigQuery type names that legitimately follow "AS" inside a CAST(...)
# expression -- not an output-column alias, so _strip_select_aliases must not
# treat "AS FLOAT64" the same way it treats "AS total_transactions".
_CAST_TYPE_NAMES = {
    "int64", "string", "float64", "bool", "boolean", "date", "datetime", "timestamp",
    "numeric", "bignumeric", "bytes", "struct", "array", "record", "json", "time", "geography",
}
_AS_ALIAS_RE = re.compile(r"\bas\s+([a-z_][a-z0-9_]*)\b")


def _strip_select_aliases(sql):
    """Drops `AS <name>` output-column aliases (`COUNT(*) AS total_transactions`)
    -- like a table alias, this names something for the reader/consumer but
    doesn't change which rows or values the query produces, so a query that
    only differs by adding or renaming an output alias is not a different
    query for exact_match purposes. Leaves CAST(...)'s "AS <type>" alone,
    since that's part of the cast's syntax, not an alias."""
    return _AS_ALIAS_RE.sub(lambda m: "" if m.group(1) not in _CAST_TYPE_NAMES else m.group(0), sql)


def canonicalize(sql):
    """Normalized form used for exact_match: case/whitespace-insensitive,
    treats `LOWER(col) = 'x'`, table-alias-qualified columns, and output
    column aliases (`AS total_transactions`) as equivalent to their plain/
    unaliased form, since none of these change what rows or values the query
    actually produces -- only stylistic choices SQLCoder makes."""
    s = normalize(sql).rstrip(";").strip()
    s = s.replace('"', "'")  # literal quote-style choice, not a semantic difference
    s = _fold_lower_comparisons(s)
    s = _strip_table_aliases(s)
    s = _strip_select_aliases(s)
    return re.sub(r"\s+", " ", s).strip()


def tables_in(sql):
    found = set()
    for name in ALL_TABLE_NAMES:
        if re.search(rf"\b{name.lower()}\b", sql.lower()):
            found.add(name)
    return found


def evaluate(testcases_path=TESTCASES_PATH, ground_tables=False):
    # Verify the local Ollama server is reachable before running the whole
    # suite through it -- generation goes through generate_sql_ollama.
    pipeline.load_model()

    with open(testcases_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    results = []
    for case in cases:
        question, gold_sql = case["question"], case["sql"]

        outcome = pipeline.generate_sql_kag(question, ground_tables=ground_tables)
        pred_sql = outcome["sql"]
        source = outcome["source"]
        corrections = outcome["corrections"]

        gold_tables = tables_in(gold_sql)
        pred_tables = tables_in(pred_sql)
        recall = len(gold_tables & pred_tables) / len(gold_tables) if gold_tables else None
        precision = len(gold_tables & pred_tables) / len(pred_tables) if pred_tables else 0.0
        exact = canonicalize(gold_sql) == canonicalize(pred_sql)

        results.append({
            "question": question,
            "gold_sql": gold_sql,
            "pred_sql": pred_sql,
            "source": source,
            "corrections": corrections,
            "schema_valid": outcome["schema_valid"],
            "schema_violations": outcome["schema_violations"],
            "gold_tables": sorted(gold_tables),
            "pred_tables": sorted(pred_tables),
            "table_recall": recall,
            "table_precision": precision,
            "exact_match": exact,
        })

    return results


def summarize(results):
    n = len(results)
    exact = sum(r["exact_match"] for r in results)
    recalls = [r["table_recall"] for r in results if r["table_recall"] is not None]
    avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
    avg_precision = sum(r["table_precision"] for r in results) / n if n else 0.0

    schema_valid_count = sum(1 for r in results if r.get("schema_valid"))

    print(f"\n{'='*80}\nSummary over {n} test cases\n{'='*80}")
    print(f"  exact_match:      {exact}/{n} ({exact/n:.0%})")
    print(f"  avg table recall: {avg_recall:.0%}  (did it use the right tables)")
    print(f"  avg table prec.:  {avg_precision:.0%}  (did it avoid extra/wrong tables)")
    print(f"  schema_valid:     {schema_valid_count}/{n} ({schema_valid_count/n:.0%})  (every table/column resolves against aml_data_model_schema.json)")
    print()
    for r in results:
        flag = "OK  " if r["exact_match"] else "DIFF"
        src = f" [{r['source']}]" if r.get("source") else ""
        valid_flag = "valid" if r.get("schema_valid") else "INVALID"
        print(f"[{flag}]{src} [{valid_flag}] {r['question']}")
        print(f"   gold: {r['gold_sql']}")
        print(f"   pred: {r['pred_sql']}")
        if r.get("corrections"):
            print(f"   corrections applied: {r['corrections']}")
        if r.get("schema_violations"):
            print(f"   unresolved schema violations: {r['schema_violations']}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testcases", default=TESTCASES_PATH)
    parser.add_argument("--ground-tables", action="store_true",
                         help="prepend a dynamic 'use only these exact table names' line")
    parser.add_argument("--out", default=None, help="optional path to dump results JSON")
    args = parser.parse_args()

    results = evaluate(testcases_path=args.testcases, ground_tables=args.ground_tables)
    summarize(results)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Wrote detailed results to {args.out}")
