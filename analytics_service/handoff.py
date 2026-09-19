"""Export the versioned, sanitized contract for the independently owned model."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .catalog import load_catalog
from .metrics import METRICS


def export_contract():
    return {"contract_version": 1, "dialect": "GoogleSQL", "synthetic_only": True,
            "definition_status": "starter definitions; pending banking domain-owner approval",
            "schema": load_catalog(), "metrics": [asdict(metric) for metric in METRICS]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = json.dumps(export_contract(), indent=2) + "\n"
    if args.output:
        args.output.write_text(value)
    else:
        print(value, end="")
