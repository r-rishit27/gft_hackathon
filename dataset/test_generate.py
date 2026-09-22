import copy
import unittest

from generate import build, validate


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema, cls.tables, cls.ledger, cls.truth, _ = build()

    def test_valid_package(self):
        self.assertGreater(validate(self.schema, self.tables), 0)

    def test_deterministic(self):
        _, tables, ledger, truth, _ = build()
        self.assertEqual(tables, self.tables)
        self.assertEqual(ledger, self.ledger)
        self.assertEqual(truth, self.truth)

    def corrupted(self, table, index, field, value):
        tables = dict(self.tables)
        tables[table] = list(tables[table])
        tables[table][index] = copy.deepcopy(tables[table][index])
        tables[table][index][field] = value
        return tables

    def test_orphan_rejected(self):
        with self.assertRaisesRegex(ValueError, "Orphan account"):
            validate(self.schema, self.corrupted("Transaction", 0, "account_id", "MISSING"))

    def test_invalid_enum_rejected(self):
        with self.assertRaisesRegex(ValueError, "Bad enum"):
            validate(self.schema, self.corrupted("Transaction", 0, "direction", "OUTBOUND"))

    def test_invalid_money_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid money"):
            validate(self.schema, self.corrupted("Transaction", 0, "normalized_booked_amount",
                {"currency_code": "USD", "units": 10, "nanos": 1_000_000_000}))

    def test_duplicate_keys_rejected(self):
        row = self.tables["Transaction"][0]
        tables = self.corrupted("Transaction", 1, "transaction_id", row["transaction_id"])
        tables["Transaction"][1]["validity_start_time"] = row["validity_start_time"]
        with self.assertRaisesRegex(ValueError, "Duplicate keys"):
            validate(self.schema, tables)


if __name__ == "__main__":
    unittest.main()
