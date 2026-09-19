"""Document declared enums separately from values observed in the generated data."""
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEMANTIC_IDS = {"party_supplementary_data_id", "risk_typology_id"}


def sql_type(field):
    aliases = {"RECORD": "STRUCT", "BOOLEAN": "BOOL", "INTEGER": "INT64", "FLOAT": "FLOAT64"}
    kind = aliases.get(field["type"], field["type"])
    if kind == "STRUCT":
        kind += "<" + ", ".join(f"{f['name']} {sql_type(f)}" for f in field["fields"]) + ">"
    return f"ARRAY<{kind}>" if field["mode"] == "REPEATED" else kind


def enrich(document, tables):
    total = 0

    def visit(fields, parents, path):
        nonlocal total
        for field in fields:
            total += 1
            name = field["name"]
            full_path = path + "." + name
            field["column_path"] = full_path
            field["sql_type"] = sql_type(field)
            field["nullable"] = field["mode"] == "NULLABLE"
            repeated = field["mode"] == "REPEATED"
            values = [v for p in parents for v in (p.get(name) or [])] if repeated else [p.get(name) for p in parents]
            present = [v for v in values if v is not None]
            stats = {"parent_record_count": len(parents), "value_count": len(values),
                     "null_count": len(values) - len(present)}
            if repeated:
                stats["empty_array_count"] = sum(not p.get(name) for p in parents)
            field["observed_values"] = stats
            declared = re.search(r"One of: \[([^\]]+)\]", field.get("description", ""))
            enum = declared.group(1).split(":") if declared else None
            enum_source = "Official input schema snapshot: field description" if declared else None
            if field["type"] in ("BOOL", "BOOLEAN"):
                enum, enum_source = [False, True], "Boolean data type"
            if full_path in ("CommercialPartiesRegistration.party_size", "RegisteredPartiesExport.party_size"):
                enum, enum_source = ["SMALL", "LARGE"], "Supplied registration schema; null for retail parties where nullable"
            if full_path == "ExportedMetadata.resource_type":
                enum = list(next(t for t in document["output_data_model"]["tables"] if t["name"] == "ExportedMetadata")["metadata_metrics_by_resource_type"])
                enum_source = "Resource types covered by supplied schema metadata_metrics_by_resource_type"
            if full_path == "ExportedMetadata.name":
                metric_map = next(t for t in document["output_data_model"]["tables"] if t["name"] == "ExportedMetadata")["metadata_metrics_by_resource_type"]
                enum = sorted({v for values in metric_map.values() for v in values})
                enum_source = "Metric names covered by supplied schema; valid names depend on resource_type"
            field["allowed_enum_values"] = enum
            field["enum_source"] = enum_source
            if field["type"] in ("RECORD", "STRUCT"):
                stats["enumeration"] = "See nested fields; complete records are not enumerated"
                visit(field["fields"], present, full_path)
                continue
            encoded = Counter(json.dumps(v, sort_keys=True) for v in present)
            unique = [json.loads(k) for k in encoded]
            stats["distinct_count"] = len(unique)
            identifier = (name.endswith("_id") and name not in SEMANTIC_IDS) or name == "obfuscated_phone"
            field["value_role"] = "identifier" if identifier else "category" if enum or name in SEMANTIC_IDS else "attribute"
            if not identifier and len(unique) <= 50 and field["type"] != "JSON":
                unique.sort()
                stats["unique_values"] = unique
                stats["unique_values_complete"] = True
                stats["value_counts"] = [{"value": v, "count": encoded[json.dumps(v, sort_keys=True)]} for v in unique]
            else:
                stats["sample_values"] = sorted(unique, key=lambda v: json.dumps(v, sort_keys=True))[:3]
                stats["unique_values_complete"] = not unique
                stats["enumeration_omitted_reason"] = "Identifier values are not enumerated" if identifier else "JSON payload or more than 50 distinct values; summarized to keep documentation readable"
            if present and field["type"] in ("INT64", "INTEGER", "FLOAT64", "FLOAT", "DATE", "TIMESTAMP"):
                stats["min"] = min(present)
                stats["max"] = max(present)
            if enum is not None:
                stats["values_outside_declared_enum"] = [v for v in unique if v not in enum]
                if stats["values_outside_declared_enum"]:
                    raise ValueError(f"Unexpected enum values in {full_path}")
    for section in ("input_data_model", "output_data_model"):
        for table in document[section]["tables"]:
            visit(table["fields"], tables[table["name"]], table["name"])
    document["value_documentation"] = {
        "source": "Local v2 data files verified against BigQuery at identifier migration completion; not a new live cloud scan",
        "field_count_including_nested": total,
        "allowed_enum_values": "Declared domain values, not inferred from a sample; null means no finite enum declared in available schema sources",
        "observed_values": "Values actually present in this synthetic dataset, not a restriction on future values",
        "nulls": "null_count is separate from unique_values and distinct_count; enum lists contain only non-null domain values",
        "nested_counts": "Repeated children count per array element. Children of null structs and empty arrays have no occurrences; parent counts are recorded separately",
        "enumeration_policy": "All non-identifier scalar fields with at most 50 distinct values list all values and frequencies. High-cardinality attributes, identifiers and JSON payloads have distinct counts and up to 3 samples; numeric/date/timestamp fields also have ranges",
        "semantic_identifier_categories": sorted(SEMANTIC_IDS),
        "json_payloads": "JSON has no fixed nested BigQuery schema; examples appear on the value field and metric names are mapped by resource type",
    }
    return document


def main():
    path = ROOT / "aml_data_model_schema.json"
    document = json.loads(path.read_text())
    tables = {t["name"]: [json.loads(line) for line in (ROOT / "data" / (t["name"] + ".jsonl")).read_text().splitlines()]
              for s in ("input_data_model", "output_data_model") for t in document[s]["tables"]}
    enrich(document, tables)
    path.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps(document["value_documentation"], indent=2))


if __name__ == "__main__":
    main()
