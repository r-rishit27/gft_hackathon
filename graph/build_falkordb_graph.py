import json
import os

from dotenv import load_dotenv
from falkordb import FalkorDB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "aml_data_model_schema.json")

HOST = os.environ["FALKORDB_HOST"]
PORT = int(os.environ["FALKORDB_PORT"])
USERNAME = os.environ["FALKORDB_USERNAME"]
PASSWORD = os.environ["FALKORDB_PASSWORD"]
GRAPH_NAME = os.environ.get("FALKORDB_GRAPH", "aml_data_model")


def field_constraints(field, is_top_level, primary_key_names):
    mode = field.get("mode", "NULLABLE")
    return {
        "required": mode == "REQUIRED",
        "nullable": mode == "NULLABLE",
        "repeated": mode == "REPEATED",
        # Only a top-level field can be (part of) a table's primary key --
        # primary_key entries are always bare field names, never dotted paths.
        "is_primary_key": is_top_level and field["name"] in primary_key_names,
    }


def upsert_field(graph, dataset, table_name, field, parent_column_path, primary_key_names):
    """Recursively upserts a Field node (and its nested RECORD fields, if
    any) keyed by its globally-unique column_path -- not by {table, name},
    since the same leaf name (e.g. "region_code") legitimately appears under
    several different parent structs within one table (addresses,
    nationalities, phone_numbers, ...) and would otherwise collapse into a
    single wrong node."""
    column_path = field.get("column_path") or (
        f"{parent_column_path}.{field['name']}" if parent_column_path else f"{table_name}.{field['name']}"
    )
    is_top_level = parent_column_path is None
    c = field_constraints(field, is_top_level, primary_key_names)

    graph.query(
        """
        MERGE (f:Field {column_path:$column_path})
        SET f.name=$name, f.table=$table, f.dataset=$dataset,
            f.type=$type, f.sql_type=$sql_type, f.mode=$mode, f.description=$description,
            f.required=$required, f.nullable=$nullable, f.repeated=$repeated,
            f.is_primary_key=$is_primary_key, f.allowed_enum_values=$allowed_enum_values,
            f.value_role=$value_role
        """,
        {
            "column_path": column_path,
            "table": table_name,
            "dataset": dataset,
            "name": field["name"],
            "type": field.get("type", ""),
            "sql_type": field.get("sql_type", ""),
            "mode": field.get("mode", ""),
            "description": field.get("description", ""),
            "allowed_enum_values": field.get("allowed_enum_values") or [],
            "value_role": field.get("value_role") or "",
            **c,
        },
    )

    if parent_column_path:
        graph.query(
            """
            MATCH (p:Field {column_path:$parent})
            MATCH (f:Field {column_path:$column_path})
            MERGE (p)-[:HAS_SUBFIELD]->(f)
            """,
            {"parent": parent_column_path, "column_path": column_path},
        )
    else:
        graph.query(
            """
            MATCH (t:Table {name:$table, dataset:$dataset})
            MATCH (f:Field {column_path:$column_path})
            MERGE (t)-[:HAS_FIELD]->(f)
            """,
            {"table": table_name, "dataset": dataset, "column_path": column_path},
        )

    for nested in field.get("fields", []) or []:
        upsert_field(graph, dataset, table_name, nested, column_path, primary_key_names)


def upsert_table(graph, dataset, table):
    pk = table.get("primary_key", [])
    graph.query(
        """
        MATCH (d:Dataset {name:$dataset})
        MERGE (t:Table {name:$name, dataset:$dataset})
        SET t.classification=$classification,
            t.destination=$destination,
            t.description=$description,
            t.primary_key=$primary_key,
            t.row_count=$row_count,
            t.bigquery_table=$bigquery_table,
            t.data_status=$data_status
        MERGE (d)-[:HAS_TABLE]->(t)
        """,
        {
            "dataset": dataset,
            "name": table["name"],
            "classification": table.get("classification", ""),
            "destination": table.get("destination", ""),
            "description": table.get("description", ""),
            "primary_key": pk,
            "row_count": table.get("row_count") or 0,
            "bigquery_table": table.get("bigquery_table", ""),
            "data_status": table.get("data_status", ""),
        },
    )
    for field in table["fields"]:
        upsert_field(graph, dataset, table["name"], field, None, pk)

    for resource_type, metrics in table.get("metadata_metrics_by_resource_type", {}).items():
        for metric in metrics:
            graph.query(
                """
                MATCH (t:Table {name:$table, dataset:$dataset})
                MERGE (m:MetadataMetric {name:$metric, resource_type:$resource_type})
                MERGE (t)-[:CATALOGUES]->(m)
                """,
                {"table": table["name"], "dataset": dataset, "resource_type": resource_type, "metric": metric},
            )


def main():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)

    db = FalkorDB(host=HOST, port=PORT, username=USERNAME, password=PASSWORD)
    graph = db.select_graph(GRAPH_NAME)

    # Start clean so re-runs don't duplicate nodes/edges.
    try:
        graph.query("MATCH (n) DETACH DELETE n")
    except Exception:
        pass  # empty/nonexistent graph

    graph.query(
        "MERGE (d:Dataset {name:$name}) SET d.description=$desc",
        {"name": "input_data_model", "desc": schema["input_data_model"]["description"]},
    )
    graph.query(
        "MERGE (d:Dataset {name:$name}) SET d.description=$desc",
        {"name": "output_data_model", "desc": schema["output_data_model"]["description"]},
    )

    for table in schema["input_data_model"]["tables"]:
        upsert_table(graph, "input_data_model", table)
    for table in schema["output_data_model"]["tables"]:
        upsert_table(graph, "output_data_model", table)

    # --- Input-internal FK relationships (Field -> Field, top-level only --
    # every entry in `relationships` references a bare field name). ---
    for rel in schema["input_data_model"].get("relationships", []):
        from_table, from_field = rel["from"].split(".", 1)
        to_table, to_field = rel["to"].split(".", 1)
        graph.query(
            """
            MATCH (a:Field {table:$ft, name:$fn, dataset:'input_data_model'})
            MATCH (b:Field {table:$tt, name:$tn, dataset:'input_data_model'})
            MERGE (a)-[r:REFERENCES]->(b)
            SET r.note=$note
            """,
            {
                "ft": from_table,
                "fn": from_field,
                "tt": to_table,
                "tn": to_field,
                "note": rel.get("note", ""),
            },
        )

    # --- Input -> Output linkages (explicit, hand-mapped for the messy free-text entries) ---
    # (from_table, from_field_or_None, to_table, to_field_or_None, note)
    linkages = [
        ("Party", "party_id", "RiskScores", "party_id",
         "Risk score is produced per party in the input Party table"),
        ("Party", "party_id", "Explainability", "party_id",
         "Explainability attributions are produced per party"),
        ("RetailPartiesRegistration", "party_id", "RegisteredPartiesExport", "party_id",
         "Registered parties export mirrors registration input, plus lifecycle metadata"),
        ("CommercialPartiesRegistration", "party_id", "RegisteredPartiesExport", "party_id",
         "Registered parties export mirrors registration input, plus lifecycle metadata"),
        ("RiskCaseEvent", "risk_typology_measurements", "ExportedMetadata", None,
         "Backtest recall metrics (ObservedRecallValuesPerTypology) are computed per risk "
         "typology defined via RiskCaseEvent"),
        ("Party", None, "ExportedMetadata", None,
         "Missingness/importance metrics are computed over feature families derived from input tables"),
        ("AccountPartyLink", None, "ExportedMetadata", None,
         "Missingness/importance metrics are computed over feature families derived from input tables"),
        ("Transaction", None, "ExportedMetadata", None,
         "Missingness/importance metrics are computed over feature families derived from input tables"),
        ("InteractionEvent", None, "ExportedMetadata", None,
         "Missingness/importance metrics are computed over feature families derived from input tables"),
        ("RiskCaseEvent", None, "ExportedMetadata", None,
         "Missingness/importance metrics are computed over feature families derived from input tables"),
        ("PartySupplementaryData", None, "ExportedMetadata", None,
         "Missingness/importance metrics are computed over feature families derived from input tables"),
    ]

    for from_table, from_field, to_table, to_field, note in linkages:
        if from_field and to_field:
            graph.query(
                """
                MATCH (a:Field {table:$ft, name:$fn, dataset:'input_data_model'})
                MATCH (b:Field {table:$tt, name:$tn, dataset:'output_data_model'})
                MERGE (a)-[r:LINKS_TO]->(b)
                SET r.note=$note
                """,
                {"ft": from_table, "fn": from_field, "tt": to_table, "tn": to_field, "note": note},
            )
        else:
            graph.query(
                """
                MATCH (a:Table {name:$ft, dataset:'input_data_model'})
                MATCH (b:Table {name:$tt, dataset:'output_data_model'})
                MERGE (a)-[r:LINKS_TO]->(b)
                SET r.note=$note
                """,
                {"ft": from_table, "tt": to_table, "note": note},
            )

    # --- sanity report ---
    counts = graph.query(
        "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY label"
    ).result_set
    rel_counts = graph.query(
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS c ORDER BY rel"
    ).result_set

    print("Nodes:")
    for label, c in counts:
        print(f"  {label}: {c}")
    print("Relationships:")
    for rel, c in rel_counts:
        print(f"  {rel}: {c}")


if __name__ == "__main__":
    main()
