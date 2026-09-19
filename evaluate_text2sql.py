"""
Evaluate gaussalgo/T5-LM-Large-text2sql-spider (optionally a fine-tuned checkpoint)
against eval_testcases.json, using the same FalkorDB retrieval + schema
serialization the API/frontend use.

Metrics (SQL-aware, not exact string match, since column/table order and
aliasing legitimately vary):
  - table_recall: fraction of gold tables referenced in the gold SQL that also
    appear in the generated SQL
  - table_precision: fraction of tables in the generated SQL that are actually
    gold tables
  - exact_match: normalized-whitespace, case-insensitive string equality
    (informative, but a poor SQL similarity metric on its own -- kept as a
    reference point, not the headline number)

Usage:
    python evaluate_text2sql.py                       # base model
    python evaluate_text2sql.py --model ./finetuned_model
"""

import argparse
import json
import os
import re

import text2sql_falkordb as pipeline

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
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


def evaluate(model_path):
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, token=pipeline.HF_TOKEN)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_path, token=pipeline.HF_TOKEN)

    with open(TESTCASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    graph = pipeline.connect_graph()

    results = []
    for case in cases:
        question, gold_sql = case["question"], case["sql"]
        tables = pipeline.retrieve_relevant_tables(graph, question, top_k=6)
        schema_string = pipeline.build_schema_string(tables)
        model_input = pipeline.build_model_input(question, schema_string, with_system_prompt=False)
        pred_sql = pipeline.generate_sql(tokenizer, model, model_input)

        gold_tables = tables_in(gold_sql)
        pred_tables = tables_in(pred_sql)
        recall = len(gold_tables & pred_tables) / len(gold_tables) if gold_tables else None
        precision = len(gold_tables & pred_tables) / len(pred_tables) if pred_tables else 0.0
        exact = normalize(gold_sql) == normalize(pred_sql)

        results.append({
            "question": question,
            "gold_sql": gold_sql,
            "pred_sql": pred_sql,
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

    print(f"\n{'='*80}\nSummary over {n} test cases\n{'='*80}")
    print(f"  exact_match:      {exact}/{n} ({exact/n:.0%})")
    print(f"  avg table recall: {avg_recall:.0%}  (did it use the right tables)")
    print(f"  avg table prec.:  {avg_precision:.0%}  (did it avoid extra/wrong tables)")
    print()
    for r in results:
        flag = "OK  " if r["exact_match"] else "DIFF"
        print(f"[{flag}] {r['question']}")
        print(f"   gold: {r['gold_sql']}")
        print(f"   pred: {r['pred_sql']}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=pipeline.MODEL_PATH)
    parser.add_argument("--out", default=None, help="optional path to dump results JSON")
    args = parser.parse_args()

    results = evaluate(args.model)
    summarize(results)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Wrote detailed results to {args.out}")
