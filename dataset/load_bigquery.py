"""Load the verified package using Cloud Shell's gcloud identity; never overwrite tables."""
import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://bigquery.googleapis.com/bigquery/v2"


class API:
    def __init__(self):
        self.token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()

    def request(self, method, path, value=None, raw=None, content_type="application/json"):
        body = raw if raw is not None else json.dumps(value).encode() if value is not None else None
        url = path if path.startswith("https://") else BASE + path
        req = urllib.request.Request(url, data=body, method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError(f"BigQuery {exc.code}: {exc.read().decode()}") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true", help="Validate package and print target without cloud access")
    parser.add_argument("--dataset", help="Optional staging dataset override")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "manifest.json").read_text())
    project, dataset, location = (manifest[k] for k in ("project", "dataset", "location"))
    if args.dataset:
        import re
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.dataset):
            raise ValueError("Invalid dataset name")
        dataset = args.dataset
    for name, info in manifest["tables"].items():
        data = (ROOT / "data" / f"{name}.jsonl").read_bytes()
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise RuntimeError(f"Data checksum mismatch: {name}")
    if args.plan:
        print(json.dumps({"target": f"{project}.{dataset}", "location": location,
                          "tables": {k: v["rows"] for k, v in manifest["tables"].items()},
                          "write_disposition": "WRITE_EMPTY", "checksums": "passed"}, indent=2))
        return
    api = API()
    dataset_path = f"/projects/{project}/datasets/{dataset}"
    existing = api.request("GET", dataset_path)
    if existing:
        if existing["location"].lower() != location.lower():
            raise RuntimeError("Existing dataset location differs; refusing to modify it")
    else:
        api.request("POST", f"/projects/{project}/datasets", {
            "datasetReference": {"projectId": project, "datasetId": dataset}, "location": location,
            "description": "Synthetic retail AML demo. Jan-Aug 2026. All outputs simulated. Not a trained AML AI dataset.",
            "labels": {"synthetic": "true", "purpose": "aml_demo"}})
    receipts = []
    for name, info in manifest["tables"].items():
        table_path = dataset_path + "/tables/" + name
        ref = {"projectId": project, "datasetId": dataset, "tableId": name}
        fields = json.loads((ROOT / "schemas" / f"{name}.json").read_text())
        fingerprint = hashlib.sha256((info["sha256"] + json.dumps(fields, sort_keys=True)).encode()).hexdigest()[:40]
        existing = api.request("GET", table_path)
        if existing and existing.get("labels", {}).get("package_hash") != fingerprint:
            raise RuntimeError(f"Unrecognized existing table {name}; refusing to overwrite or append")
        if not existing:
            existing = api.request("POST", dataset_path + "/tables", {
                "tableReference": ref, "schema": {"fields": fields},
                "labels": {"synthetic": "true", "package_hash": fingerprint},
                "description": "SYNTHETIC DEMO DATA. Outputs are simulated, not AML AI predictions. " +
                    ("Intentionally empty: retail-only dataset." if info["rows"] == 0 else "Seed 20260919; see local manifest for assumptions.")})
        actual = int(existing.get("numRows", 0))
        if actual not in (0, info["rows"]):
            raise RuntimeError(f"Unexpected row count in {name}: {actual}")
        job_id = None
        if actual == 0 and info["rows"]:
            job_id = f"{dataset}_{name}_{fingerprint}"
            job_path = f"/projects/{project}/jobs/{job_id}?location={location}"
            job = api.request("GET", job_path)
            if not job:
                config = {"jobReference": {"projectId": project, "jobId": job_id, "location": location},
                          "configuration": {"load": {"destinationTable": ref, "schema": {"fields": fields},
                              "sourceFormat": "NEWLINE_DELIMITED_JSON", "writeDisposition": "WRITE_EMPTY",
                              "createDisposition": "CREATE_NEVER", "maxBadRecords": 0, "ignoreUnknownValues": False}}}
                boundary = "aml_package_20260919"
                payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                           + json.dumps(config).encode()
                           + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                           + (ROOT / "data" / f"{name}.jsonl").read_bytes()
                           + f"\r\n--{boundary}--\r\n".encode())
                job = api.request("POST", f"https://bigquery.googleapis.com/upload/bigquery/v2/projects/{project}/jobs?uploadType=multipart",
                                  raw=payload, content_type=f"multipart/related; boundary={boundary}")
            deadline = time.monotonic() + 600
            while job["status"]["state"] != "DONE":
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Load still running: {job_id}. Rerun to resume.")
                time.sleep(2)
                job = api.request("GET", job_path)
            if "errorResult" in job["status"]:
                raise RuntimeError(json.dumps(job["status"]))
        verified = api.request("GET", table_path)
        if int(verified.get("numRows", 0)) != info["rows"]:
            raise RuntimeError(f"Post-load count mismatch: {name}")
        receipt = {"table": f"{project}.{dataset}.{name}", "rows": info["rows"], "job_id": job_id,
                   "expirationTime": verified.get("expirationTime")}
        receipts.append(receipt)
        print(f"VERIFIED {name}: {info['rows']} rows", flush=True)
        (ROOT / "reports/cloud_load_receipt.json").write_text(json.dumps(receipts, indent=2) + "\n")
    query = (ROOT / "verify_bigquery.sql").read_text().replace(".aml_demo.", f".{dataset}.")
    result = api.request("POST", f"/projects/{project}/queries", {
        "query": query, "useLegacySql": False, "location": location, "timeoutMs": 20000,
        "maximumBytesBilled": "100000000"})
    if not result.get("jobComplete"):
        ref = result["jobReference"]
        for _ in range(60):
            time.sleep(2)
            result = api.request("GET", f"/projects/{project}/queries/{ref['jobId']}?location={location}")
            if result.get("jobComplete"):
                break
        else:
            raise TimeoutError("Verification query still running")
    values = {row["f"][0]["v"]: int(row["f"][1]["v"]) for row in result.get("rows", [])}
    (ROOT / "reports/cloud_validation.json").write_text(json.dumps(values, indent=2) + "\n")
    if not values or any(values.values()):
        raise RuntimeError(f"Cloud validation failed: {values}")
    print("CLOUD_VALIDATION_PASSED " + json.dumps(values), flush=True)
    print(f"COMPLETE: https://console.cloud.google.com/bigquery?project={project}&ws=!1m4!1m3!3m2!1s{project}!2s{dataset}", flush=True)


if __name__ == "__main__":
    main()
