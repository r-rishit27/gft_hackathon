"""Provision the explicitly approved two-role local PoC; never modify source data."""
import argparse
import json
import secrets
import time

import google.auth
from google.api_core.exceptions import Forbidden
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from .bigquery_client import scoped_client
from .catalog import ROOT
from .config import LOCATION, PROJECT, Settings, Scope
from .password_auth import hash_password
from .setup_local import grant, inspect, request, secure_json

ROLE_TABLES = {
    "monitoring": ["Party", "AccountPartyLink", "Transaction", "InteractionEvent", "PartySupplementaryData", "RetailPartiesRegistration", "CommercialPartiesRegistration"],
    "investigation": ["Party", "RiskCaseEvent", "RiskScores", "Explainability"],
}


def verify_scope(scope, all_resources):
    client = scoped_client(scope)
    report = {}
    try:
        for name, resource in all_resources.items():
            try:
                client.query(f"SELECT COUNT(*) FROM `{resource['view']}`", job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, maximum_bytes_billed=100_000_000), timeout=30).result(timeout=30)
                allowed = True
            except Forbidden:
                allowed = False
            if allowed != (name in scope.resources):
                raise RuntimeError(f"Unexpected BigQuery access: {name}, allowed={allowed}")
            report[name] = "allowed" if allowed else "denied"
            if allowed:
                permissions = client.test_iam_permissions(resource["view"], ["bigquery.tables.getData", "bigquery.tables.updateData", "bigquery.tables.update", "bigquery.tables.delete"], timeout=30).get("permissions", [])
                if set(permissions) != {"bigquery.tables.getData"}:
                    raise RuntimeError(f"Unexpected read/write permissions on {name}: {permissions}")
        report["writes_on_allowed_views"] = "denied"
        try:
            client.query(f"SELECT COUNT(*) FROM `{PROJECT}.aml_demo.Party`", job_config=bigquery.QueryJobConfig(dry_run=True), timeout=30).result(timeout=30)
        except Forbidden:
            report["direct_source"] = "denied"
        else:
            raise RuntimeError("Role principal unexpectedly has direct source access")
    finally:
        client.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    base = json.loads((ROOT / "analytics_service/config.local.json").read_text())
    resources = base["scopes"]["demo"]["resources"]
    config_path = ROOT / "analytics_service/config.roles.local.json"
    password_path = ROOT / "analytics_service/profile-logins.local.json"
    if args.verify_only:
        configured = Settings.from_file(str(config_path))
        report = {role: verify_scope(scope, resources) for role, scope in configured.scopes.items()}
        report_path = ROOT / "analytics_service/role-access-verification.local.json"
        if not report_path.exists():
            secure_json(report_path, report)
        print(json.dumps(report, indent=2))
        return
    if config_path.exists() or password_path.exists():
        raise RuntimeError("Role configuration already exists; refusing to overwrite credentials")
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    with bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION) as client:
        inspect(client)
        for name in set(sum(ROLE_TABLES.values(), [])):
            view = client.get_table(resources[name]["view"])
            expected = f"SELECT {', '.join('`' + c + '`' for c in resources[name]['columns'])} FROM `{PROJECT}.aml_demo.{name}`"
            if view.table_type != "VIEW" or view.view_query != expected or view.location != LOCATION:
                raise RuntimeError(f"Unexpected existing authorized view: {name}")
        print(json.dumps({"roles": ROLE_TABLES, "source_and_views_match": True}))
        if not args.apply:
            return
        with AuthorizedSession(credentials.with_quota_project(None)) as session:
            user = request(session, "GET", "https://openidconnect.googleapis.com/v1/userinfo")
        if not user.get("email_verified"):
            raise RuntimeError("A verified Google user is required")
        new = {**base, "identities": [], "scopes": {}, "history_path": str(ROOT / "analytics_service/workspace.local.sqlite")}
        logins = {}
        with AuthorizedSession(credentials) as session:
            for role, tables in ROLE_TABLES.items():
                account_id = "aml-" + role + "-poc"
                account = f"{account_id}@{PROJECT}.iam.gserviceaccount.com"
                url = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{account}"
                response = session.get(url, timeout=30)
                if response.status_code == 404:
                    request(session, "POST", f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts", json={"accountId": account_id, "serviceAccount": {"displayName": "AML " + role + " read-only PoC"}})
                elif not response.ok:
                    raise RuntimeError("Cannot inspect role principal")
                for attempt in range(5):
                    try:
                        grant(session, f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}", "roles/bigquery.jobUser", "serviceAccount:" + account)
                        break
                    except RuntimeError as exc:
                        if attempt == 4 or account not in str(exc) or "does not exist" not in str(exc):
                            raise
                        time.sleep(5)
                grant(session, url, "roles/iam.serviceAccountTokenCreator", "user:" + user["email"])
                for name in tables:
                    policy = client.get_iam_policy(resources[name]["view"])
                    binding = next((b for b in policy.bindings if b["role"] == "roles/bigquery.dataViewer" and not b.get("condition")), None)
                    if binding is None:
                        binding = {"role": "roles/bigquery.dataViewer", "members": []}
                        policy.bindings.append(binding)
                    member = "serviceAccount:" + account
                    if member not in binding["members"]:
                        binding["members"] = list(binding["members"]) + [member]
                        client.set_iam_policy(resources[name]["view"], policy)
                new["scopes"][role] = {"entities": base["scopes"]["demo"]["entities"], "service_account": account, "resources": {name: resources[name] for name in tables}}
                password = secrets.token_urlsafe(18)
                new["identities"].append({"subject": "poc-" + role, "username": role, "password_hash": hash_password(password), "scope": role})
                logins[role] = {"username": role, "password": password, "tables": tables}
        Settings.model_validate(new)
        # Save once before verification so transient IAM propagation does not lose credentials.
        secure_json(password_path, logins)
        secure_json(config_path, new)
        print(f"Private local login details: {password_path}")
        print(f"Role configuration: {config_path}")
        report = {role: verify_scope(Scope.model_validate(scope), resources) for role, scope in new["scopes"].items()}
        secure_json(ROOT / "analytics_service/role-access-verification.local.json", report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
