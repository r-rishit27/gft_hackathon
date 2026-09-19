import json
import os

from dotenv import load_dotenv
from falkordb import FalkorDB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

SCHEMA_PATH = os.path.join(SCRIPT_DIR, "aml_data_model_schema.json")

HOST = os.environ["FALKORDB_HOST"]
PORT = int(os.environ["FALKORDB_PORT"])
USERNAME = os.environ["FALKORDB_USERNAME"]
PASSWORD = os.environ["FALKORDB_PASSWORD"]
GRAPH_NAME = os.environ.get("FALKORDB_GRAPH", "aml_data_model")


def field_constraints(field, primary_key_names):
    mode = field.get("mode", "NULLABLE")
    return {
        "required": mode == "REQUIRED",
        "nullable": mode == "NULLABLE",
        "repeated": mode == "REPEATED",
        "is_primary_key": field["name"] in primary_key_names,
    }


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

    # --- Datasets ---
    graph.query(
        "MERGE (d:Dataset {name:$name}) SET d.description=$desc",
        {"name": "input_data_model", "desc": schema["input_data_model"]["description"]},
    )
    graph.query(
        "MERGE (d:Dataset {name:$name}) SET d.description=$desc",
        {"name": "output_data_model", "desc": schema["output_data_model"]["description"]},
    )

    # --- Input tables + fields ---
    for table in schema["input_data_model"]["tables"]:
        pk = table.get("primary_key", [])
        graph.query(
            """
            MATCH (d:Dataset {name:'input_data_model'})
            MERGE (t:Table {name:$name, dataset:'input_data_model'})
            SET t.classification=$classification,
                t.description=$description,
                t.primary_key=$primary_key
            MERGE (d)-[:HAS_TABLE]->(t)
            """,
            {
                "name": table["name"],
                "classification": table.get("classification", ""),
                "description": table.get("description", ""),
                "primary_key": pk,
            },
        )
        for field in table["fields"]:
            c = field_constraints(field, pk)
            graph.query(
                """
                MATCH (t:Table {name:$table, dataset:'input_data_model'})
                MERGE (f:Field {name:$name, table:$table, dataset:'input_data_model'})
                SET f.type=$type, f.mode=$mode, f.description=$description,
                    f.required=$required, f.nullable=$nullable,
                    f.repeated=$repeated, f.is_primary_key=$is_primary_key
                MERGE (t)-[:HAS_FIELD]->(f)
                """,
                {
                    "table": table["name"],
                    "name": field["name"],
                    "type": field.get("type", ""),
                    "mode": field.get("mode", ""),
                    "description": field.get("description", ""),
                    **c,
                },
            )

    # --- Output tables + fields (including nested subfields) ---
    for table in schema["output_data_model"]["tables"]:
        graph.query(
            """
            MATCH (d:Dataset {name:'output_data_model'})
            MERGE (t:Table {name:$name, dataset:'output_data_model'})
            SET t.destination=$destination, t.description=$description
            MERGE (d)-[:HAS_TABLE]->(t)
            """,
            {
                "name": table["name"],
                "destination": table.get("destination", ""),
                "description": table.get("description", ""),
            },
        )
        for field in table["fields"]:
            c = field_constraints(field, [])
            graph.query(
                """
                MATCH (t:Table {name:$table, dataset:'output_data_model'})
                MERGE (f:Field {name:$name, table:$table, dataset:'output_data_model'})
                SET f.type=$type, f.mode=$mode, f.description=$description,
                    f.required=$required, f.nullable=$nullable, f.repeated=$repeated
                MERGE (t)-[:HAS_FIELD]->(f)
                """,
                {
                    "table": table["name"],
                    "name": field["name"],
                    "type": field.get("type", ""),
                    "mode": field.get("mode", ""),
                    "description": field.get("description", ""),
                    **c,
                },
            )
            for sub in field.get("subfields", []):
                sc = field_constraints(sub, [])
                graph.query(
                    """
                    MATCH (f:Field {name:$parent, table:$table, dataset:'output_data_model'})
                    MERGE (sf:Field {name:$name, table:$table, dataset:'output_data_model',
                                     parent_field:$parent})
                    SET sf.type=$type, sf.mode=$mode, sf.description=$description,
                        sf.required=$required, sf.nullable=$nullable, sf.repeated=$repeated
                    MERGE (f)-[:HAS_SUBFIELD]->(sf)
                    """,
                    {
                        "table": table["name"],
                        "parent": field["name"],
                        "name": sub["name"],
                        "type": sub.get("type", ""),
                        "mode": sub.get("mode", ""),
                        "description": sub.get("description", ""),
                        **sc,
                    },
                )

        # metadata metrics catalogue (ExportedMetadata only)
        for resource_type, metrics in table.get("metadata_metrics_by_resource_type", {}).items():
            for metric in metrics:
                graph.query(
                    """
                    MATCH (t:Table {name:$table, dataset:'output_data_model'})
                    MERGE (m:MetadataMetric {name:$metric, resource_type:$resource_type})
                    MERGE (t)-[:CATALOGUES]->(m)
                    """,
                    {"table": table["name"], "resource_type": resource_type, "metric": metric},
                )

    # --- Input-internal FK relationships (Field -> Field) ---
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
