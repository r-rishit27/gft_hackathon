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
    (informative, but a poor SQL similarity metric on its own -- kept as a
    reference point, not the headline number)

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


def tables_in(sql):
    found = set()
    for name in ALL_TABLE_NAMES:
        if re.search(rf"\b{name.lower()}\b", sql.lower()):
            found.add(name)
    return found


def evaluate(model_path, testcases_path=TESTCASES_PATH, ground_tables=False):
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, token=pipeline.HF_TOKEN)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_path, token=pipeline.HF_TOKEN)

    with open(testcases_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    results = []
    for case in cases:
        question, gold_sql = case["question"], case["sql"]

        outcome = pipeline.generate_sql_kag(
            question, ground_tables=ground_tables, tokenizer=tokenizer, model=model
        )
        pred_sql = outcome["sql"]
        source = outcome["source"]
        corrections = outcome["corrections"]

        gold_tables = tables_in(gold_sql)
        pred_tables = tables_in(pred_sql)
        recall = len(gold_tables & pred_tables) / len(gold_tables) if gold_tables else None
        precision = len(gold_tables & pred_tables) / len(pred_tables) if pred_tables else 0.0
        exact = normalize(gold_sql) == normalize(pred_sql)

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
    parser.add_argument("--model", default=pipeline.DEFAULT_MODEL_PATH)
    parser.add_argument("--testcases", default=TESTCASES_PATH)
    parser.add_argument("--ground-tables", action="store_true",
                         help="prepend a dynamic 'use only these exact table names' line")
    parser.add_argument("--out", default=None, help="optional path to dump results JSON")
    args = parser.parse_args()

    results = evaluate(args.model, testcases_path=args.testcases, ground_tables=args.ground_tables)
    summarize(results)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Wrote detailed results to {args.out}")
