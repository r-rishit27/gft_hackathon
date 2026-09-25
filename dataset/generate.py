"""Deterministic synthetic AML demo data. Standard library only."""
import calendar
import copy
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, tzinfo=timezone.utc)
SEED = 20260919
COUNTRIES = [
    ("HK", "Hong Kong", "Hong Kong", "HKD", "7.8"),
    ("GB", "London", "England", "GBP", "0.8"),
    ("IN", "Mumbai", "Maharashtra", "INR", "85"),
    ("TW", "Taipei", "Taipei", "TWD", "32"),
    ("FR", "Paris", "Ile-de-France", "EUR", "0.92"),
    ("PL", "Warsaw", "Mazowieckie", "PLN", "4"),
    ("IE", "Dublin", "Leinster", "EUR", "0.92"),
]


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def money(cents, currency="USD"):
    return {"currency_code": currency, "units": cents // 100,
            "nanos": (cents % 100) * 10_000_000}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n")


def schemas():
    original = json.loads((ROOT / "reference/project_schema.json").read_text())
    official = json.loads((ROOT / "reference/official_input_schema.json").read_text())
    result, changes = {}, []
    for section in ("input_data_model", "output_data_model"):
        for table in original[section]["tables"]:
            fields = []
            lookup = {f["name"]: f for f in official.get(table["name"], [])}
            for source in table["fields"]:
                name = source["name"]
                if table["name"] == "Party" and name == "civil_status_code":
                    changes.append("Party.civil_status_code omitted: optional field is STRUCT in supplied file but STRING in official schema; pending confirmation.")
                    continue
                f = copy.deepcopy(lookup.get(name, source))
                if f["mode"] != source["mode"]:
                    changes.append(f"{table['name']}.{name}: retaining supplied {source['mode']} mode (official {f['mode']}); generated values satisfy both.")
                    f["mode"] = source["mode"]
                if "subfields" in f:
                    f["fields"] = f.pop("subfields")
                fields.append(f)

            def clean(items):
                return [{**{k: v for k, v in f.items() if k in ("name", "mode", "description")},
                         "type": "RECORD" if f["type"] == "STRUCT" else f["type"],
                         **({"fields": clean(f["fields"])} if "fields" in f else {})} for f in items]

            result[table["name"]] = clean(fields)
    return result, changes


def build_legacy():
    rng = random.Random(SEED)
    schema, changes = schemas()
    tables = {name: [] for name in schema}
    customers, ledger, truth = {}, [], []
    risky = set(rng.sample(range(1, 851), 90))
    case_people = list(sorted(risky)) + rng.sample([i for i in range(1, 851) if i not in risky], 30)
    case_index = {p: i for i, p in enumerate(case_people)}
    exit_people = set(case_people[:20]) | set(rng.sample(range(851, 1001), 20))
    alerts = ["AML_ALERT_GOOGLE", "AML_ALERT_LEGACY", "AML_ALERT_ADHOC", "AML_ALERT_EXPLORATORY", "AML_ALERT_EXTERNAL"]
    for i in range(1, 1001):
        pid, aid = f"SYN_P{i:04d}", f"SYN_A{i:04d}"
        country = COUNTRIES[(i - 1) % 7]
        join = START - timedelta(days=365 + i % 120) if i <= 850 else START + timedelta(days=rng.randrange(0, 180))
        valid = START if join < START else join
        exit_at = datetime(2026, 8, 15 + i % 15, tzinfo=timezone.utc) if i in exit_people else END
        ci = case_index.get(i)
        onset = START + timedelta(days=35 + (ci % 6) * 27) if ci is not None else None
        pattern = ["cash_burst", "rapid_movement", "cross_border_burst"][ci % 3] if i in risky else "ordinary"
        address = {"address_line": f"Synthetic address {i}", "street": "Demo Street", "building_number": str(i),
                   "town": country[1], "subregion": country[2], "region_code": country[0]}
        row = dict(party_id=pid, validity_start_time=stamp(valid), is_entity_deleted=False,
                   source_system="SYNTHETIC_DEMO", type="CONSUMER", name=f"Synthetic Customer {i:04d}",
                   addresses=[address], birth_date=f"{1960 + i % 45}-06-15", establishment_date=None,
                   occupation=rng.choice(["Teacher", "Engineer", "Nurse", "Retail worker", "Accountant"]),
                   gender=rng.choice(["FEMALE", "MALE", "UNSPECIFIED"]), nationalities=[{"region_code": country[0]}],
                   residencies=[{"region_code": country[0]}], exit_date=None, join_date=join.date().isoformat(),
                   assets_value_range={"start_amount": money(1_000_000), "end_amount": money(10_000_000)},
                   phone_numbers=[{"type": "PERSONAL", "obfuscated_phone": f"SYN_PHONE_{i:04d}", "region_code": country[0]}],
                   email_addresses=[{"type": "PERSONAL", "email": f"customer{i:04d}@example.invalid", "region_code": country[0]}],
                   education_level_code="UNKNOWN")
        tables["Party"].append(row)
        if i % 10 == 0:
            changed = copy.deepcopy(row)
            changed["validity_start_time"] = stamp(max(valid + timedelta(days=1), START + timedelta(days=190)))
            changed["addresses"][0]["street"] = "Updated Demo Street"
            tables["Party"].append(changed)
            row = changed
        if i in exit_people:
            departed = copy.deepcopy(row)
            departed.update(validity_start_time=stamp(exit_at), exit_date=exit_at.date().isoformat())
            tables["Party"].append(departed)
        tables["AccountPartyLink"].append(dict(account_id=aid, party_id=pid, validity_start_time=stamp(valid),
                                                is_entity_deleted=False, role="PRIMARY_HOLDER", source_system="SYNTHETIC_DEMO"))
        for field, value in [("declared_monthly_income_usd", float(rng.randrange(1500, 9000))), ("expected_monthly_transactions", 40.0)]:
            tables["PartySupplementaryData"].append(dict(party_supplementary_data_id=field, party_id=pid,
                validity_start_time=stamp(valid), is_entity_deleted=False, source_system="SYNTHETIC_DEMO",
                supplementary_data_payload={"value": value}))
        tables["RetailPartiesRegistration"].append({"party_id": pid, "party_size": None})
        tables["RegisteredPartiesExport"].append(dict(party_id=pid, party_size=None,
            earliest_remove_time=stamp(valid + timedelta(days=365)), party_with_prediction_intent="true",
            registration_or_uptier_time=stamp(valid)))
        customers[i] = dict(pid=pid, aid=aid, country=country, valid=valid, end=exit_at,
                            onset=onset, pattern=pattern)
        for j in range(12):
            when = valid + timedelta(seconds=rng.randrange(int((exit_at - valid).total_seconds())))
            tables["InteractionEvent"].append(dict(interaction_event_id=f"SYN_I{i:04d}_{j:02d}", party_id=pid,
                account_id=aid, type="LOGIN", event_time=stamp(when),
                ip_address={"type": "V4", "ip": f"192.0.2.{1+i%254}", "region_code": country[0], "is_vpn": False}))
        truth.append({"party_id": pid, "scenario": pattern, "scenario_start": stamp(onset) if onset else None,
                      "has_case": ci is not None, "simulated_sar": ci is not None and ci < 45})

    def transaction(i, when, cents, kind, direction, scenario="ordinary", foreign=False):
        p = customers[i]
        tid = f"SYN_T{len(tables['Transaction'])+1:06d}"
        region = p["country"][0]
        cp_region = rng.choice([c[0] for c in COUNTRIES if c[0] != region]) if foreign else region
        tables["Transaction"].append(dict(transaction_id=tid, validity_start_time=stamp(when),
            is_entity_deleted=False, source_system="SYNTHETIC_DEMO", type=kind, direction=direction,
            account_id=p["aid"], counterparty_account={"account_id": None,
                "counterparty_name": f"Synthetic external counterparty {rng.randrange(1, 501):04d}",
                "addresses": [], "region_code": cp_region}, book_time=stamp(when),
            normalized_booked_amount=money(cents), ip_address={"type": "V4", "ip": f"192.0.2.{1+i%254}",
                "region_code": region, "is_vpn": False}))
        rate = Decimal(p["country"][4])
        original = (Decimal(cents) / 100 * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ledger.append({"transaction_id": tid, "original_currency": p["country"][3],
                       "original_amount": str(original), "local_units_per_usd": str(rate),
                       "fx_basis": "fictional fixed demo rate, not historical market data", "scenario": scenario})

    for i in sorted(risky):
        p = customers[i]
        for j in range(20):
            when = p["onset"] + timedelta(hours=j * 3)
            if p["pattern"] == "cash_burst":
                transaction(i, when, rng.randrange(250_000, 450_000), "CASH", "CREDIT", p["pattern"])
            elif p["pattern"] == "rapid_movement":
                transaction(i, when, 500_000, "WIRE", "CREDIT", p["pattern"])
                transaction(i, when + timedelta(minutes=15), 495_000, "WIRE", "DEBIT", p["pattern"], True)
            else:
                transaction(i, when, rng.randrange(800_000, 1_800_000), "WIRE", "DEBIT", p["pattern"], True)
    while len(tables["Transaction"]) < 50_000:
        i = rng.randrange(1, 1001)
        p = customers[i]
        when = p["valid"] + timedelta(seconds=rng.randrange(int((p["end"] - p["valid"]).total_seconds())))
        kind = rng.choices(["CARD", "WIRE", "CASH", "CHECK", "OTHER", "CRYPTO"], [50, 28, 15, 4, 2, 1])[0]
        direction = "DEBIT" if kind == "CARD" else rng.choice(["DEBIT", "CREDIT"])
        cents = max(100, min(700_000, int(rng.lognormvariate(9, 1.4))))
        transaction(i, when, cents, kind, direction, foreign=kind == "WIRE" and rng.random() < 0.12)

    for ci, i in enumerate(case_people):
        p = customers[i]
        onset = p["onset"]
        process = onset + timedelta(days=5)
        events = [(process - timedelta(hours=2), alerts[ci % len(alerts)]), (process, "AML_PROCESS_START")]
        if i in risky:
            events += [(onset, "AML_SUSPICIOUS_ACTIVITY_START"), (onset + timedelta(days=3), "AML_SUSPICIOUS_ACTIVITY_END")]
        if ci < 80:
            events.append((process + timedelta(days=7 + ci % 20), "AML_PROCESS_END"))
        if ci < 45:
            events.append((process + timedelta(days=5), "AML_SAR"))
        if ci < 20:
            events.append((p["end"], "AML_EXIT"))
        for j, (when, kind) in enumerate(sorted(events)):
            tables["RiskCaseEvent"].append(dict(risk_case_event_id=f"SYN_E{ci:03d}_{j}",
                event_time=stamp(when), type=kind, party_id=p["pid"], risk_case_id=f"SYN_C{ci:03d}",
                risk_typology_measurements=[{"risk_typology_id": p["pattern"]}] if i in risky else []))

    for month in range(1, 9):
        period = datetime(2026, month + 1, 1, tzinfo=timezone.utc)
        for i, p in customers.items():
            if not p["valid"] < period <= p["end"]:
                continue
            active_risk = i in risky and p["onset"] < period
            score = round(rng.uniform(0.80, 0.98) if active_risk else rng.betavariate(2, 9), 6)
            tables["RiskScores"].append(dict(party_id=p["pid"], risk_period_end_time=stamp(period), risk_score=score))
            tables["Explainability"].append(dict(party_id=p["pid"], risk_period_end_time=stamp(period),
                attributions=[{"feature": "unusual_wire_credit_activity", "attribution": round(score * 0.5, 6)},
                              {"feature": "demo_cash_activity", "attribution": round(score * 0.3, 6)},
                              {"feature": "demo_transaction_velocity", "attribution": round(score * 0.2, 6)}]))
        for resource, metric, value in [
            ("PredictionResults", "Missingness", {"featureFamilies": [{"featureFamily": "unusual_wire_credit_activity", "missingnessValue": 0.0}]}),
            ("BacktestResults", "ObservedRecallValues", {"recallValues": [{"partyInvestigationsPerPeriod": 100,
                "partiesCount": 100, "identifiedPartiesCount": 80, "recallValue": 0.8, "scoreThreshold": 0.8}]})]:
            tables["ExportedMetadata"].append(dict(resource_type=resource, resource_id=f"SIMULATED_{resource}_2026{month:02d}",
                                                     name=metric, value=json.dumps(value)))
    return schema, tables, ledger, truth, changes


def entity_prefix(country):
    return ("HASE" if country == "HK" else "HSBC") + "_" + country


def build():
    # Transform after generation so random draws and all financial values stay identical.
    schema, tables, ledger, truth, changes = build_legacy()
    prefixes = {r["party_id"]: entity_prefix(r["residencies"][0]["region_code"]) for r in tables["Party"]}
    accounts = {r["account_id"]: prefixes[r["party_id"]] for r in tables["AccountPartyLink"]}
    mapping = {}
    for name, rows in tables.items():
        for row in rows:
            prefix = prefixes.get(row.get("party_id"), accounts.get(row.get("account_id")))
            if prefix:
                for key, value in row.items():
                    if key.endswith("_id") and isinstance(value, str) and value.startswith("SYN_"):
                        mapping[value] = prefix + value[3:]

    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    for name, rows in tables.items():
        for row in rows:
            prefix = prefixes.get(row.get("party_id"), accounts.get(row.get("account_id")))
            if "source_system" in row:
                row["source_system"] = prefix + "_CORE"
            if name == "Party":
                number = row["party_id"].split("P")[-1]
                row["name"] = prefix + "_CUSTOMER_" + number
                for phone in row["phone_numbers"]:
                    phone["obfuscated_phone"] = prefix + "_PHONE_" + number
                for address in row["addresses"]:
                    address["address_line"] = prefix + "_ADDRESS_" + number
            if name == "Transaction":
                cp = row["counterparty_account"]
                cp["counterparty_name"] = "External " + cp["region_code"] + " Counterparty " + cp["counterparty_name"].split()[-1]
        tables[name] = replace(rows)
    return schema, tables, replace(ledger), replace(truth), changes


def validate(schema, tables):
    checks = 0

    def check(condition, message):
        nonlocal checks
        checks += 1
        if not condition:
            raise ValueError(message)

    def record(row, fields, path):
        check(set(row) <= {f["name"] for f in fields}, f"Unknown field: {path}")
        for field in fields:
            name, kind, mode = field["name"], field["type"], field["mode"]
            value = row.get(name)
            label = f"{path}.{name}"
            if value is None:
                check(mode == "NULLABLE", f"Missing {label}")
                continue
            values = value if mode == "REPEATED" else [value]
            check(isinstance(values, list), f"Not array: {label}")
            for v in values:
                if kind == "RECORD":
                    check(isinstance(v, dict) and bool(field.get("fields")), f"Invalid struct: {label}")
                    record(v, field["fields"], label)
                elif kind in ("STRING", "DATE", "TIMESTAMP", "JSON"):
                    check(isinstance(v, str), f"Not string: {label}")
                    if kind == "TIMESTAMP":
                        check(parse(v).tzinfo is not None, f"Missing timezone {label}")
                    if kind == "DATE":
                        datetime.strptime(v, "%Y-%m-%d")
                    if kind == "JSON":
                        json.loads(v)
                elif kind == "INT64":
                    check(type(v) is int, f"Not integer: {label}")
                elif kind == "FLOAT64":
                    check(type(v) in (int, float), f"Not number: {label}")
                elif kind == "BOOL":
                    check(type(v) is bool, f"Not bool: {label}")
                enum = re.search(r"One of: \[([^\]]+)\]", field.get("description", ""))
                if enum:
                    check(v in enum.group(1).split(":"), f"Bad enum: {label}")

    for name, rows in tables.items():
        for row in rows:
            record(row, schema[name], name)
    primary = {"Party": ["party_id", "validity_start_time"], "AccountPartyLink": ["account_id", "party_id", "validity_start_time"],
               "Transaction": ["transaction_id", "validity_start_time"], "RiskCaseEvent": ["risk_case_event_id"],
               "InteractionEvent": ["interaction_event_id"], "PartySupplementaryData": ["party_supplementary_data_id", "party_id", "validity_start_time"],
               "RiskScores": ["party_id", "risk_period_end_time"], "Explainability": ["party_id", "risk_period_end_time"],
               "RetailPartiesRegistration": ["party_id"], "CommercialPartiesRegistration": ["party_id"],
               "RegisteredPartiesExport": ["party_id"], "ExportedMetadata": ["resource_type", "resource_id", "name"]}
    for name, keys in primary.items():
        check(len({tuple(r[k] for k in keys) for r in tables[name]}) == len(tables[name]), f"Duplicate keys {name}")
    parties = defaultdict(list)
    for p in tables["Party"]:
        parties[p["party_id"]].append(p)
    first = {p: min(parse(r["validity_start_time"]) for r in rows) for p, rows in parties.items()}
    exits = {p: parse(r["exit_date"] + "T00:00:00Z") for p, rows in parties.items() for r in rows if r["exit_date"]}
    accounts = {r["account_id"]: r for r in tables["AccountPartyLink"]}
    check(len(parties) == 1000, "Customer count")
    check(len(tables["Transaction"]) == 50000, "Transaction count")
    check(not tables["CommercialPartiesRegistration"], "Retail-only dataset")
    for name, rows in tables.items():
        for r in rows:
            if "party_id" in r:
                pid = r["party_id"]
                check(pid in parties, f"Orphan party in {name}")
                time = r.get("validity_start_time", r.get("event_time", r.get("risk_period_end_time")))
                if time:
                    check(parse(time) >= first[pid], f"Pre-join row {name}")
            if "account_id" in r:
                check(r["account_id"] in accounts, f"Orphan account {name}")
    for r in tables["Transaction"]:
        account = accounts[r["account_id"]]
        booked = parse(r["book_time"])
        check(START <= booked < END, "Transaction outside range")
        check(parse(account["validity_start_time"]) <= booked < exits.get(account["party_id"], END), "Inactive account transaction")
        check(parse(r["validity_start_time"]) >= booked, "Transaction version precedes posting")
        amount = r["normalized_booked_amount"]
        check(amount["currency_code"] == "USD" and amount["units"] >= 0 and 0 <= amount["nanos"] < 1_000_000_000, "Invalid money")
    for r in tables["InteractionEvent"]:
        check(first[r["party_id"]] <= parse(r["event_time"]) < exits.get(r["party_id"], END), "Inactive interaction")
    cases = defaultdict(dict)
    for r in tables["RiskCaseEvent"]:
        check(START <= parse(r["event_time"]) < END, "Case event outside range")
        cases[r["risk_case_id"]][r["type"]] = parse(r["event_time"])
    for events in cases.values():
        check("AML_PROCESS_START" in events, "Case without start")
        if "AML_PROCESS_END" in events:
            check(events["AML_PROCESS_END"] >= events["AML_PROCESS_START"], "Case end before start")
        if "AML_SAR" in events:
            check(events["AML_SAR"] >= events["AML_PROCESS_START"], "SAR before investigation")
    score_keys = {(r["party_id"], r["risk_period_end_time"]) for r in tables["RiskScores"]}
    check(score_keys == {(r["party_id"], r["risk_period_end_time"]) for r in tables["Explainability"]}, "Score/explanation mismatch")
    for r in tables["RiskScores"]:
        check(0 <= r["risk_score"] <= 1, "Score outside range")
        check(parse(r["risk_period_end_time"]) <= exits.get(r["party_id"], END), "Score after exit")
    return checks


def main():
    schema, tables, ledger, truth, changes = build()
    checks = validate(schema, tables)
    for directory in ("data", "schemas", "reports", "companion"):
        (ROOT / directory).mkdir(exist_ok=True)
    files = {}
    for name, rows in tables.items():
        write_json(ROOT / "schemas" / f"{name}.json", schema[name])
        path = ROOT / "data" / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        files[name] = {"rows": len(rows), "bytes": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    for name, rows in [("transaction_currency_and_scenarios", ledger), ("scenario_ground_truth", truth)]:
        (ROOT / "companion" / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    counts = Counter(r["type"] for r in tables["RiskCaseEvent"])
    countries = Counter(r["residencies"][0]["region_code"] for r in tables["Party"] if r["validity_start_time"] == min(p["validity_start_time"] for p in tables["Party"] if p["party_id"] == r["party_id"]))
    manifest = {"project": "gen-lang-client-0810987953", "dataset": "aml_demo", "location": "asia-south1",
                "seed": SEED, "synthetic": True, "start": stamp(START), "end_exclusive": stamp(END),
                "reporting_currency": "USD", "countries": dict(countries), "tables": files,
                "schema_decisions": changes, "actual_aml_ai_ready": False,
                "limitations": ["Eight months is insufficient for full AML AI training/tuning; verify engine-specific duration, location, labels, registration and service access before use.",
                    "Outputs and metadata are simulated, not actual AML AI results or measured model performance.",
                    "CommercialPartiesRegistration is intentionally empty for retail-only scope.",
                    "Local-currency details use fictional FX rates and live in companion files, not the official input tables.",
                    "Optional civil_status_code omitted pending schema conflict decision."]}
    write_json(ROOT / "manifest.json", manifest)
    document = json.loads((ROOT / "reference/project_schema.json").read_text())
    document["dataset"] = {k: v for k, v in manifest.items() if k != "tables"}
    document["dataset"]["identifier_format"] = "<ENTITY>_<COUNTRY>_<TYPE><NUMBER>"
    document["dataset"]["entity_country_mapping"] = {c[0]: entity_prefix(c[0]) for c in COUNTRIES}
    document["dataset"]["omitted_columns"] = {"Party.civil_status_code": "Not deployed: supplied STRUCT conflicts with official STRING; optional field omitted."}
    document["dataset"]["naming_notes"] = ["UK is encoded as GB.", "Prefixes reflect the owning customer country, not the counterparty country.", "External counterparties are not represented as HSBC or HASE accounts.", "Dataset-wide SIMULATED resource IDs are retained.", "All entities are fictional; codes do not imply real bank records."]
    for section in ("input_data_model", "output_data_model"):
        for table in document[section]["tables"]:
            old_fields = {f["name"]: f for f in table["fields"]}
            table["fields"] = copy.deepcopy(schema[table["name"]])
            table["row_count"] = files[table["name"]]["rows"]
            table["bigquery_table"] = f"{manifest['project']}.{manifest['dataset']}.{table['name']}"
            table["data_status"] = "intentionally_empty_retail_only" if not table["row_count"] else "synthetic"
            for field in table["fields"]:
                original = old_fields.get(field["name"], {})
                if "references" in original:
                    field["references"] = original["references"]
                if tables[table["name"]]:
                    field["example"] = tables[table["name"]][0].get(field["name"])
            table["documentation_notes"] = "Nested children use BigQuery fields notation. Schema types and modes match deployment; examples are synthetic."
    from enrich_schema import enrich
    write_json(ROOT / "aml_data_model_schema.json", enrich(document, tables))
    write_json(ROOT / "reports/validation.json", {"status": "passed", "checks": checks, "row_counts": {k: len(v) for k, v in tables.items()},
               "event_counts": dict(counts), "cases": counts["AML_PROCESS_START"], "closed_cases": counts["AML_PROCESS_END"],
               "open_cases": counts["AML_PROCESS_START"] - counts["AML_PROCESS_END"], "sar_cases": counts["AML_SAR"],
               "transaction_value_usd": str(sum(Decimal(r["normalized_booked_amount"]["units"]) + Decimal(r["normalized_booked_amount"]["nanos"]) / 10**9 for r in tables["Transaction"]))})
    print(json.dumps({"status": "passed", "checks": checks, "tables": {k: len(v) for k, v in tables.items()}}, indent=2))


if __name__ == "__main__":
    main()
