import json
import unittest
from pathlib import Path

from migrate_identifiers import canonical, schema_shape


class MigrationTests(unittest.TestCase):
    def test_wire_format_equivalence(self):
        schema = [
            {"name": "when", "type": "TIMESTAMP", "mode": "REQUIRED"},
            {"name": "amount", "type": "INT64", "mode": "REQUIRED"},
            {"name": "optional", "type": "STRING", "mode": "NULLABLE"},
            {"name": "children", "type": "RECORD", "mode": "REPEATED", "fields": []},
        ]
        local = {"when": "2026-01-01T00:00:00Z", "amount": 12, "children": []}
        api = {"when": "2026-01-01T00:00:00.000000+00:00", "amount": "12", "optional": None, "children": []}
        self.assertEqual(canonical(local, schema), canonical(api, schema))
        api["amount"] = "13"
        self.assertNotEqual(canonical(local, schema), canonical(api, schema))

    def test_schema_aliases_only(self):
        local = [{"name": "a", "type": "INT64", "mode": "REQUIRED"}]
        api = [{"name": "a", "type": "INTEGER", "mode": "REQUIRED"}]
        self.assertEqual(schema_shape(local), schema_shape(api))
        api[0]["mode"] = "NULLABLE"
        self.assertNotEqual(schema_shape(local), schema_shape(api))

    def test_delivered_document_matches_generated_schemas(self):
        root = Path(__file__).resolve().parent
        document = json.loads((root / "aml_data_model_schema.json").read_text())
        manifest = json.loads((root / "manifest.json").read_text())
        count = 0
        for section in ("input_data_model", "output_data_model"):
            for table in document[section]["tables"]:
                schema = json.loads((root / "schemas" / (table["name"] + ".json")).read_text())
                self.assertEqual(schema_shape(table["fields"]), schema_shape(schema))
                self.assertEqual(table["row_count"], manifest["tables"][table["name"]]["rows"])
                count += 1
        self.assertEqual(count, 12)


if __name__ == "__main__":
    unittest.main()
