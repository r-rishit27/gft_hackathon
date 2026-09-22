import json
import unittest
from pathlib import Path


class SchemaValuesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        doc = json.loads((Path(__file__).resolve().parent / "aml_data_model_schema.json").read_text())
        cls.fields = {}
        def visit(fields):
            for field in fields:
                cls.fields[field["column_path"]] = field
                visit(field.get("fields", []))
        for section in ("input_data_model", "output_data_model"):
            for table in doc[section]["tables"]:
                visit(table["fields"])

    def test_direction_domain_and_counts(self):
        field = self.fields["Transaction.direction"]
        self.assertEqual(set(field["allowed_enum_values"]), {"DEBIT", "CREDIT"})
        self.assertEqual(set(field["observed_values"]["unique_values"]), {"DEBIT", "CREDIT"})
        self.assertEqual(sum(x["count"] for x in field["observed_values"]["value_counts"]), 50000)

    def test_unobserved_enums_are_retained(self):
        field = self.fields["Party.type"]
        self.assertIn("COMPANY", field["allowed_enum_values"])
        self.assertEqual(field["observed_values"]["unique_values"], ["CONSUMER"])

    def test_nested_countries_and_type(self):
        field = self.fields["Party.residencies.region_code"]
        self.assertEqual(set(field["observed_values"]["unique_values"]), {"HK", "GB", "IN", "TW", "FR", "PL", "IE"})
        self.assertEqual(self.fields["Party.residencies"]["sql_type"], "ARRAY<STRUCT<region_code STRING>>")

    def test_identifiers_not_enumerated(self):
        field = self.fields["Transaction.transaction_id"]["observed_values"]
        self.assertNotIn("unique_values", field)
        self.assertEqual(field["distinct_count"], 50000)
        self.assertEqual(len(field["sample_values"]), 3)

    def test_empty_table_and_nulls(self):
        field = self.fields["CommercialPartiesRegistration.party_size"]
        self.assertEqual(field["allowed_enum_values"], ["SMALL", "LARGE"])
        self.assertEqual(field["observed_values"]["value_count"], 0)
        self.assertEqual(self.fields["RetailPartiesRegistration.party_size"]["observed_values"]["null_count"], 1000)


if __name__ == "__main__":
    unittest.main()
