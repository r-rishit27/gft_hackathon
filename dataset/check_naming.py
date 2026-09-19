"""Check v2 names and prove that reverse mapping reproduces every original value."""
import json
from pathlib import Path
from generate import build, build_legacy, entity_prefix, validate

ROOT = Path(__file__).resolve().parent


def main():
    schema, new, ledger, truth, _ = build()
    _, old, old_ledger, old_truth, _ = build_legacy()
    reverse = {}
    for table, rows in new.items():
        assert len(rows) == len(old[table]), table
        for before, after in zip(old[table], rows):
            for k, v in after.items():
                if k.endswith("_id") and isinstance(v, str) and before[k].startswith("SYN_"):
                    assert v not in reverse or reverse[v] == before[k]
                    reverse[v] = before[k]

    def restore(value):
        if isinstance(value, dict):
            return {k: restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [restore(v) for v in value]
        return reverse.get(value, value) if isinstance(value, str) else value

    prefixes = {r["party_id"]: entity_prefix(r["residencies"][0]["region_code"]) for r in new["Party"]}
    accounts = {r["account_id"]: prefixes[r["party_id"]] for r in new["AccountPartyLink"]}
    for table, rows in new.items():
        baseline = [json.loads(line) for line in (ROOT / "baseline/data" / f"{table}.jsonl").read_text().splitlines()]
        assert old[table] == baseline, f"Legacy generator no longer matches original {table}"
        for before, after in zip(old[table], rows):
            prefix = prefixes.get(after.get("party_id"), accounts.get(after.get("account_id")))
            reverted = restore(after)
            for k, v in after.items():
                if k.endswith("_id") and isinstance(v, str) and v in reverse:
                    assert v.startswith(prefix + "_"), (table, k, v)
            if "source_system" in after:
                assert after["source_system"] == prefix + "_CORE"
                reverted["source_system"] = before["source_system"]
            if table == "Party":
                assert after["name"].startswith(prefix + "_CUSTOMER_")
                reverted["name"] = before["name"]
                for index, phone in enumerate(after["phone_numbers"]):
                    assert phone["obfuscated_phone"].startswith(prefix + "_PHONE_")
                    reverted["phone_numbers"][index]["obfuscated_phone"] = before["phone_numbers"][index]["obfuscated_phone"]
                for index, address in enumerate(after["addresses"]):
                    assert address["address_line"].startswith(prefix + "_ADDRESS_")
                    reverted["addresses"][index]["address_line"] = before["addresses"][index]["address_line"]
            if table == "Transaction":
                cp = after["counterparty_account"]
                assert cp["counterparty_name"].startswith("External " + cp["region_code"] + " Counterparty ")
                reverted["counterparty_account"]["counterparty_name"] = before["counterparty_account"]["counterparty_name"]
            assert reverted == before, f"Non-naming change in {table}"
            assert "SYN" not in json.dumps(after), f"Old synthetic value in {table}"
    assert restore(ledger) == old_ledger
    assert restore(truth) == old_truth
    checks = validate(schema, new)
    report = {"status": "passed", "non_naming_values_unchanged": True, "baseline_matches_original": True,
              "all_prefixes_match_owner_country": True, "old_SYN_values_remaining": 0,
              "unique_identifier_mappings": len(reverse), "schema_and_relationship_checks": checks,
              "companion_files_consistent": True}
    (ROOT / "reports/naming_validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
