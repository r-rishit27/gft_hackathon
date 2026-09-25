"""Verified, resumable v2 naming migration. No DML; backups precede replacements."""
import collections
import json
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from load_bigquery import API, ROOT

PROJECT = "gen-lang-client-0810987953"
LIVE = "aml_demo"
STAGE = "aml_demo_stage_20260919_v2"
BACKUP = "aml_demo_backup_20260919_v1"
LOCATION = "asia-south1"
ALIASES = {"BOOL": "BOOLEAN", "INT64": "INTEGER", "FLOAT64": "FLOAT", "STRUCT": "RECORD"}


def schema_shape(fields):
    return [{"name": f["name"], "type": ALIASES.get(f["type"], f["type"]), "mode": f.get("mode", "NULLABLE"),
             **({"fields": schema_shape(f["fields"])} if "fields" in f else {})} for f in fields]


def canonical(row, fields):
    output = {}
    for f in fields:
        value = row.get(f["name"])
        if value is None:
            output[f["name"]] = [] if f["mode"] == "REPEATED" else None
            continue
        def convert(v):
            kind = ALIASES.get(f["type"], f["type"])
            if kind == "RECORD":
                return canonical(v, f["fields"])
            if kind == "TIMESTAMP":
                return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            if kind == "INTEGER":
                return int(v)
            if kind == "FLOAT":
                return float(v)
            if kind == "JSON":
                return json.loads(v) if isinstance(v, str) else v
            return v
        output[f["name"]] = [convert(v) for v in value] if f["mode"] == "REPEATED" else convert(value)
    return output


def signature(row, fields):
    return json.dumps(canonical(row, fields), sort_keys=True, separators=(",", ":"))


def query_rows(api, sql):
    result = api.request("POST", f"/projects/{PROJECT}/queries", {
        "query": sql, "useLegacySql": False, "location": LOCATION, "timeoutMs": 20000,
        "maxResults": 1000, "maximumBytesBilled": "1000000000"})
    job = result["jobReference"]["jobId"]
    endpoint = f"/projects/{PROJECT}/queries/{job}?location={LOCATION}&maxResults=1000"
    deadline = time.monotonic() + 300
    while not result.get("jobComplete"):
        if time.monotonic() > deadline:
            raise TimeoutError("Read-only verification query timeout")
        time.sleep(1)
        result = api.request("GET", endpoint)
    while True:
        for row in result.get("rows", []):
            yield json.loads(row["f"][0]["v"])
        if not result.get("pageToken"):
            break
        result = api.request("GET", endpoint + "&pageToken=" + urllib.parse.quote(result["pageToken"], safe=""))


def table_path(dataset, name):
    return f"/projects/{PROJECT}/datasets/{dataset}/tables/{name}"


def verify_table(api, dataset, name, base):
    schema = json.loads((base / "schemas" / f"{name}.json").read_text())
    meta = api.request("GET", table_path(dataset, name))
    if not meta or schema_shape(meta["schema"]["fields"]) != schema_shape(schema):
        raise RuntimeError(f"Schema mismatch {dataset}.{name}")
    expected = collections.Counter(signature(json.loads(line), schema) for line in (base / "data" / f"{name}.jsonl").read_text().splitlines())
    actual = collections.Counter(signature(row, schema) for row in query_rows(api, f"SELECT TO_JSON_STRING(t) FROM `{PROJECT}.{dataset}.{name}` t"))
    if expected != actual:
        raise RuntimeError(f"Unexpected contents {dataset}.{name}: missing={sum((expected-actual).values())}, extra={sum((actual-expected).values())}; stopping")
    print(f"EXACT_MATCH {dataset}.{name} {sum(actual.values())}", flush=True)
    return meta


def ensure_dataset(api, name):
    path = f"/projects/{PROJECT}/datasets/{name}"
    existing = api.request("GET", path)
    if existing:
        if existing["location"].lower() != LOCATION:
            raise RuntimeError("Dataset location mismatch")
        return
    api.request("POST", f"/projects/{PROJECT}/datasets", {
        "datasetReference": {"projectId": PROJECT, "datasetId": name}, "location": LOCATION,
        "description": "Synthetic AML identifier migration backup or staging; 2026-09-19.", "labels": {"synthetic": "true"}})


def copy_table(api, source, destination, name, replace=False):
    job_id = f"aml_names_v2_{source}_{destination}_{name}"
    path = f"/projects/{PROJECT}/jobs/{job_id}?location={LOCATION}"
    job = api.request("GET", path)
    if not job:
        ref = lambda d: {"projectId": PROJECT, "datasetId": d, "tableId": name}
        job = api.request("POST", f"/projects/{PROJECT}/jobs", {
            "jobReference": {"projectId": PROJECT, "jobId": job_id, "location": LOCATION},
            "configuration": {"copy": {"sourceTable": ref(source), "destinationTable": ref(destination),
                "writeDisposition": "WRITE_TRUNCATE" if replace else "WRITE_EMPTY"}}})
    deadline = time.monotonic() + 300
    while job["status"]["state"] != "DONE":
        if time.monotonic() > deadline:
            raise TimeoutError(f"Copy still running {job_id}; rerun migration to resume")
        time.sleep(1)
        job = api.request("GET", path)
    if "errorResult" in job["status"]:
        raise RuntimeError(json.dumps(job["status"]))


def main():
    api = API()
    names = list(json.loads((ROOT / "manifest.json").read_text())["tables"])
    live_dataset = api.request("GET", f"/projects/{PROJECT}/datasets/{LIVE}")
    if not live_dataset or live_dataset["location"].lower() != LOCATION:
        raise RuntimeError("Live dataset missing or wrong location")
    checkpoint = ROOT / "reports/migration_checkpoint.json"
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"metadata": {}, "replaced": []}
    for name in state["metadata"]:
        if name not in state["replaced"]:
            job_id = f"aml_names_v2_{STAGE}_{LIVE}_{name}"
            job = api.request("GET", f"/projects/{PROJECT}/jobs/{job_id}?location={LOCATION}")
            if job and job.get("status", {}).get("state") == "DONE" and "errorResult" not in job["status"]:
                state["replaced"].append(name)
    # Always inspect live contents, including previously replaced tables on resume.
    for name in names:
        base = ROOT if name in state["replaced"] else ROOT / "baseline"
        meta = verify_table(api, LIVE, name, base)
        if name not in state["metadata"]:
            state["metadata"][name] = meta
    checkpoint.write_text(json.dumps(state, indent=2))
    ensure_dataset(api, BACKUP)
    for name in names:
        if not api.request("GET", table_path(BACKUP, name)):
            if name in state["replaced"]:
                raise RuntimeError("Required baseline backup missing")
            copy_table(api, LIVE, BACKUP, name)
        verify_table(api, BACKUP, name, ROOT / "baseline")
    print("BACKUP_VERIFIED", flush=True)
    subprocess.run([sys.executable, str(ROOT / "load_bigquery.py"), "--dataset", STAGE], check=True)
    for name in names:
        verify_table(api, STAGE, name, ROOT)
    print("STAGING_VERIFIED", flush=True)
    for name in names:
        if name not in state["replaced"]:
            verify_table(api, LIVE, name, ROOT / "baseline")
            copy_table(api, STAGE, LIVE, name, replace=True)
            # Record the replacement before metadata updates so resuming checks new data.
            state["replaced"].append(name)
            checkpoint.write_text(json.dumps(state, indent=2))
        old = state["metadata"][name]
        stage_meta = api.request("GET", table_path(STAGE, name))
        patch = {"description": old.get("description", "") + " Entity-country identifiers v2.",
                 "labels": {**old.get("labels", {}), **stage_meta.get("labels", {}), "naming_version": "v2"},
                 "expirationTime": old.get("expirationTime")}
        api.request("PATCH", table_path(LIVE, name), patch)
        final = verify_table(api, LIVE, name, ROOT)
        if final.get("expirationTime") != old.get("expirationTime"):
            raise RuntimeError("Expiration was not preserved")
        if schema_shape(final["schema"]["fields"]) != schema_shape(old["schema"]["fields"]):
            raise RuntimeError("Original schema not preserved")
        document = json.loads((ROOT / "aml_data_model_schema.json").read_text())
        documented = next(t for s in ("input_data_model", "output_data_model") for t in document[s]["tables"] if t["name"] == name)
        if schema_shape(final["schema"]["fields"]) != schema_shape(documented["fields"]):
            raise RuntimeError("Delivered schema documentation differs from deployment")
        print(f"REPLACED_VERIFIED {name}", flush=True)
    report = {"status": "passed", "dataset": f"{PROJECT}.{LIVE}", "backup": f"{PROJECT}.{BACKUP}",
              "staging": f"{PROJECT}.{STAGE}", "exact_row_comparisons": "passed for baseline, backup, staging and final tables",
              "schema_and_expiration_preserved": True, "documentation_matches_deployment": True,
              "replaced_tables": state["replaced"]}
    (ROOT / "reports/migration_cloud.json").write_text(json.dumps(report, indent=2))
    print("MIGRATION_COMPLETE " + json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
