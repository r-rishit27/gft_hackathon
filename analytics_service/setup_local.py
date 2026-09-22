"""Inspect the live demo; optionally provision read-only authorized views and IAM.

Run without --apply first. Never changes source tables, rows, billing, or models.
"""
import argparse
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from .catalog import ROOT, load_catalog
from .config import DATASET, ENTITIES, LOCATION, PROJECT, Settings

VIEW_DATASET = "aml_analytics_demo"
ACCOUNT_ID = "aml-analytics-demo"
ACCOUNT = f"{ACCOUNT_ID}@{PROJECT}.iam.gserviceaccount.com"
EXCLUDED = {"addresses", "phone_numbers", "email_addresses", "ip_address", "birth_date"}


def signature(fields):
    aliases = {"RECORD": "STRUCT", "INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
    return [(f["name"], aliases.get(f["type"], f["type"]), f.get("mode", "NULLABLE"),
             signature(f.get("fields", []))) for f in fields]


def inspect(client):
    catalog = load_catalog()
    manifest = json.loads((ROOT / "dataset/manifest.json").read_text())
    dataset = client.get_dataset(f"{PROJECT}.{DATASET}")
    if dataset.location != LOCATION:
        raise RuntimeError("Live dataset region differs from the approved region")
    actual = {t.table_id for t in client.list_tables(dataset)}
    if actual != set(catalog["tables"]):
        raise RuntimeError("Live table inventory differs from the catalog; review before provisioning")
    report = {}
    for name, spec in catalog["tables"].items():
        table = client.get_table(f"{PROJECT}.{DATASET}.{name}")
        if table.table_type != "TABLE" or table.num_rows != manifest["tables"][name]["rows"]:
            raise RuntimeError(f"Unexpected live table type or row count: {name}")
        if signature([f.to_api_repr() for f in table.schema]) != signature(spec["fields"]):
            raise RuntimeError(f"Unexpected live schema: {name}")
        report[name] = {"rows": table.num_rows, "schema_matches": True}
    return catalog, report


def request(session, method, url, **kwargs):
    # API activation is eventually consistent. Retry only explicit disabled-API
    # responses, never permission denials or ambiguous successful mutations.
    for delay in (5, 10, 20, 0):
        response = session.request(method, url, timeout=60, **kwargs)
        try:
            details = response.json().get("error", {}).get("details", [])
        except ValueError:
            details = []
        if delay and response.status_code == 403 and any(d.get("reason") == "SERVICE_DISABLED" for d in details):
            time.sleep(delay)
            continue
        break
    if not response.ok:
        # Only the API error message, never credential-bearing request objects.
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise RuntimeError(f"Cloud setup request failed: HTTP {response.status_code} at {url.split('?')[0]}: {message}")
    return response.json() if response.content else {}


def grant(session, url, role, member):
    policy = request(session, "POST", url + ":getIamPolicy", json={"options": {"requestedPolicyVersion": 3}})
    bindings = policy.setdefault("bindings", [])
    matching = next((b for b in bindings if b["role"] == role and not b.get("condition")), None)
    if matching is None:
        matching = {"role": role, "members": []}
        bindings.append(matching)
    if member in matching["members"]:
        return
    matching["members"].append(member)
    request(session, "POST", url + ":setIamPolicy", json={"policy": policy})


def secure_json(path, value):
    # Refuse replacement so a repeated setup cannot silently invalidate an active token.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    client = bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION)
    catalog, report = inspect(client)
    print(json.dumps({"project": PROJECT, "dataset": DATASET, "location": LOCATION, "tables": report}, indent=2))
    if not args.apply:
        print("Inspection passed. No cloud resources or credentials were changed.")
        return
    config_path = ROOT / "analytics_service/config.local.json"
    token_path = ROOT / "analytics_service/access-token.local.json"
    if config_path.exists() or token_path.exists():
        raise RuntimeError("Local configuration already exists; preserve it and review instead of overwriting")
    session = AuthorizedSession(credentials)
    # OpenID identity lookup is not a Cloud resource/quota request.
    with AuthorizedSession(credentials.with_quota_project(None)) as identity_session:
        identity = request(identity_session, "GET", "https://openidconnect.googleapis.com/v1/userinfo")
    if not identity.get("email_verified") or not identity.get("email"):
        raise RuntimeError("A verified Google user identity is required")
    for service in ("iam.googleapis.com", "iamcredentials.googleapis.com", "cloudresourcemanager.googleapis.com"):
        api = f"https://serviceusage.googleapis.com/v1/projects/{PROJECT}/services/{service}"
        if request(session, "GET", api).get("state") != "ENABLED":
            operation = request(session, "POST", api + ":enable", json={})
            for _ in range(60):
                state = request(session, "GET", "https://serviceusage.googleapis.com/v1/" + operation["name"])
                if state.get("done"):
                    if state.get("error"):
                        raise RuntimeError(f"{service} could not be enabled")
                    break
                time.sleep(2)
            else:
                raise RuntimeError(f"{service} enablement timed out")
    account_url = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{ACCOUNT}"
    account_response = session.get(account_url, timeout=30)
    if account_response.status_code == 404:
        request(session, "POST", f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts",
                json={"accountId": ACCOUNT_ID, "serviceAccount": {"displayName": "AML local analytics read-only demo"}})
    elif not account_response.ok:
        raise RuntimeError("Cannot inspect analytics service account")
    view_dataset = bigquery.Dataset(f"{PROJECT}.{VIEW_DATASET}")
    view_dataset.location = LOCATION
    view_dataset.description = "Authorized analytical views over synthetic AML demo data. No production banking data."
    view_dataset = client.create_dataset(view_dataset, exists_ok=True)
    if view_dataset.location != LOCATION:
        raise RuntimeError("Existing authorized-view dataset is in the wrong location")
    resources = {}
    from google.api_core.exceptions import NotFound
    for name, spec in catalog["tables"].items():
        columns = [f["name"] for f in spec["fields"] if f["name"] not in EXCLUDED]
        query = f"SELECT {', '.join('`' + c + '`' for c in columns)} FROM `{PROJECT}.{DATASET}.{name}`"
        view_id = f"{PROJECT}.{VIEW_DATASET}.{name}"
        try:
            existing = client.get_table(view_id)
            if existing.table_type != "VIEW" or existing.view_query != query:
                raise RuntimeError(f"Existing view differs from expected definition: {name}")
        except NotFound:
            view = bigquery.Table(view_id)
            view.view_query = query
            view.description = "Synthetic demo data. All seven approved entity-country scopes; contact fields excluded."
            client.create_table(view)
        resources[name] = {"view": view_id, "columns": columns}
    source = client.get_dataset(f"{PROJECT}.{DATASET}")
    access = list(source.access_entries)
    for name in resources:
        entry = bigquery.AccessEntry(None, "view", {"projectId": PROJECT, "datasetId": VIEW_DATASET, "tableId": name})
        if entry not in access:
            access.append(entry)
    source.access_entries = access
    client.update_dataset(source, ["access_entries"])
    views = client.get_dataset(f"{PROJECT}.{VIEW_DATASET}")
    entry = bigquery.AccessEntry("READER", "userByEmail", ACCOUNT)
    if entry not in views.access_entries:
        views.access_entries = list(views.access_entries) + [entry]
        client.update_dataset(views, ["access_entries"])
    grant(session, f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}",
          "roles/bigquery.jobUser", f"serviceAccount:{ACCOUNT}")
    grant(session, account_url, "roles/iam.serviceAccountTokenCreator", "user:" + identity["email"])
    token = secrets.token_urlsafe(40)
    config = {"query_mode": "freeform", "model_timeout_seconds": 240,
              "maximum_bytes_billed": 100_000_000, "max_concurrent_queries": 1,
              "identities": [{"subject": identity["email"], "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "scope": "demo"}],
              "scopes": {"demo": {"entities": sorted(ENTITIES), "service_account": ACCOUNT, "resources": resources}}}
    Settings.model_validate(config)
    secure_json(token_path, {"access_token": token})
    secure_json(config_path, config)
    print(f"Provisioned authorized views. Private local configuration: {config_path}")
    print(f"Access token stored locally (not printed): {token_path}")
    client.close()
    session.close()


if __name__ == "__main__":
    main()
