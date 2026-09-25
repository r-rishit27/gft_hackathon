import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "dataset/aml_data_model_schema.json"


def load_catalog(path: Path = SCHEMA_PATH) -> dict:
    source = json.loads(path.read_text())

    def field(value):
        result = {k: value[k] for k in ("name", "type", "mode", "sql_type")}
        if value.get("allowed_enum_values") is not None:
            result["allowed_enum_values"] = value["allowed_enum_values"]
        if value.get("fields"):
            result["fields"] = [field(child) for child in value["fields"]]
        return result

    tables = {
        table["name"]: {"fields": [field(f) for f in table["fields"]]}
        for section in ("input_data_model", "output_data_model")
        for table in source[section]["tables"]
    }
    canonical = json.dumps(tables, sort_keys=True).encode()
    return {"version": hashlib.sha256(canonical).hexdigest(), "tables": tables}


if __name__ == "__main__":
    # Explicit export to stdout; excludes examples, observed values and row data.
    print(json.dumps(load_catalog(), indent=2))
