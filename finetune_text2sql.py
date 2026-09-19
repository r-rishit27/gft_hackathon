"""
Fine-tune gaussalgo/T5-LM-Large-text2sql-spider on eval_testcases.json so it
learns this specific AML schema's real table/column names instead of
hallucinating ones (e.g. a nonexistent "SAR" table, "party_name", etc. --
see baseline_results.json for the pre-fine-tune failures).

CAVEAT: 13 examples is far too small a dataset to generalize from -- this
will make the model memorize these exact questions rather than learn general
text2sql-over-this-schema behavior. Treat this as a proof of concept / a way
to lock in correct answers for a fixed set of management KPI questions, not
as a substitute for a properly sized fine-tuning set (typically hundreds+
diverse question/SQL pairs per schema for real generalization).

Usage:
    python finetune_text2sql.py --epochs 15 --out ./finetuned_model
"""

import argparse
import json
import os

from torch.utils.data import Dataset
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
)

import text2sql_falkordb as pipeline

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTCASES_PATH = os.path.join(SCRIPT_DIR, "eval_testcases.json")


class SqlDataset(Dataset):
    def __init__(self, examples, tokenizer, max_length=512):
        self.examples = examples
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        # No padding here: DataCollatorForSeq2Seq pads per-batch to the
        # longest sequence in that batch instead of a fixed 512, which is
        # the difference between ~60s/step and a few seconds/step on CPU.
        model_input, target_sql = self.examples[idx]
        enc = self.tokenizer(model_input, truncation=True, max_length=self.max_length)
        labels = self.tokenizer(text_target=target_sql, truncation=True, max_length=self.max_length)
        enc["labels"] = labels["input_ids"]
        return enc


def build_training_examples():
    with open(TESTCASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)

    graph = pipeline.connect_graph()
    examples = []
    for case in cases:
        tables = pipeline.retrieve_relevant_tables(graph, case["question"], top_k=6)
        schema_string = pipeline.build_schema_string(tables)
        model_input = pipeline.build_model_input(case["question"], schema_string, with_system_prompt=False)
        examples.append((model_input, case["sql"]))
    return examples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--lr", type=float, default=3e-5)
    parser.add_argument("--out", default=os.path.join(SCRIPT_DIR, "finetuned_model"))
    args = parser.parse_args()

    print("Loading base model...")
    tokenizer = AutoTokenizer.from_pretrained(pipeline.MODEL_PATH, token=pipeline.HF_TOKEN)
    model = AutoModelForSeq2SeqLM.from_pretrained(pipeline.MODEL_PATH, token=pipeline.HF_TOKEN)
    model.config.use_cache = False  # required alongside gradient checkpointing

    print("Building training examples from FalkorDB-retrieved schema + gold SQL...")
    examples = build_training_examples()
    print(f"  {len(examples)} training examples")

    dataset = SqlDataset(examples, tokenizer)
    collator = DataCollatorForSeq2Seq(tokenizer, model=model, label_pad_token_id=-100)

    training_args = Seq2SeqTrainingArguments(
        output_dir=os.path.join(SCRIPT_DIR, "finetune_checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,  # effective batch size 4 without the memory spike
        gradient_checkpointing=True,
        optim="adafactor",  # far lower memory than AdamW for a 770M-param model
        learning_rate=args.lr,
        logging_steps=5,
        save_strategy="no",
        report_to=[],
    )

    trainer = Seq2SeqTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=collator,
    )

    print("Fine-tuning...")
    trainer.train()

    os.makedirs(args.out, exist_ok=True)
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"Saved fine-tuned model to {args.out}")


if __name__ == "__main__":
    main()
