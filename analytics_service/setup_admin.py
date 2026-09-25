"""Add a read-only Admin PoC identity with the exact union of the two role scopes."""
import argparse
import json
import os
import secrets
import tempfile
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from .catalog import ROOT
from .config import LOCATION, PROJECT, Settings
from .password_auth import hash_password
from .setup_local import grant, request, secure_json
from .setup_roles import verify_scope


def replace_private_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".admin-", suffix=".local.json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config_path = ROOT / "analytics_service/config.roles.local.json"
    login_path = ROOT / "analytics_service/profile-logins.local.json"
    config = json.loads(config_path.read_text())
    logins = json.loads(login_path.read_text())
    resources = {}
    entities = set()
    for role in ("monitoring", "investigation"):
        entities.update(config["scopes"][role]["entities"])
        for name, resource in config["scopes"][role]["resources"].items():
            if name in resources and resources[name] != resource:
                raise RuntimeError("Role view definitions differ; review before creating Admin")
            resources[name] = resource
    if "admin" not in config["scopes"]:
        print(json.dumps({"role": "admin", "tables": sorted(resources), "entities": sorted(entities), "read_only": True}))
        if not args.apply:
            return
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        account = f"aml-admin-poc@{PROJECT}.iam.gserviceaccount.com"
        url = f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts/{account}"
        with AuthorizedSession(credentials.with_quota_project(None)) as session:
            user = request(session, "GET", "https://openidconnect.googleapis.com/v1/userinfo")
        if not user.get("email_verified"):
            raise RuntimeError("Verified Google user required")
        with AuthorizedSession(credentials) as session, bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION) as client:
            for name, resource in resources.items():
                view = client.get_table(resource["view"])
                expected = f"SELECT {', '.join('`' + c + '`' for c in resource['columns'])} FROM `{PROJECT}.aml_demo.{name}`"
                if view.table_type != "VIEW" or view.location != LOCATION or view.view_query != expected:
                    raise RuntimeError(f"Existing authorized view differs: {name}")
            existing = session.get(url, timeout=30)
            if existing.status_code == 404:
                request(session, "POST", f"https://iam.googleapis.com/v1/projects/{PROJECT}/serviceAccounts", json={"accountId": "aml-admin-poc", "serviceAccount": {"displayName": "AML Admin read-only PoC"}})
            elif not existing.ok:
                raise RuntimeError("Cannot inspect Admin principal")
            for attempt in range(5):
                try:
                    grant(session, f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}", "roles/bigquery.jobUser", "serviceAccount:" + account)
                    break
                except RuntimeError as exc:
                    if attempt == 4 or account not in str(exc) or "does not exist" not in str(exc):
                        raise
                    time.sleep(5)
            grant(session, url, "roles/iam.serviceAccountTokenCreator", "user:" + user["email"])
            for resource in resources.values():
                policy = client.get_iam_policy(resource["view"])
                binding = next((b for b in policy.bindings if b["role"] == "roles/bigquery.dataViewer" and not b.get("condition")), None)
                if binding is None:
                    binding = {"role": "roles/bigquery.dataViewer", "members": []}
                    policy.bindings.append(binding)
                member = "serviceAccount:" + account
                if member not in binding["members"]:
                    binding["members"] = list(binding["members"]) + [member]
                    client.set_iam_policy(resource["view"], policy)
        password = secrets.token_urlsafe(18)
        config["scopes"]["admin"] = {"entities": sorted(entities), "service_account": account, "resources": resources}
        config["identities"].append({"subject": "poc-admin", "username": "admin", "password_hash": hash_password(password), "scope": "admin"})
        Settings.model_validate(config)
        for path in (config_path, login_path):
            backup = path.with_name(path.stem.replace(".local", "") + ".pre-admin.local.json")
            if not backup.exists():
                secure_json(backup, json.loads(path.read_text()))
        logins["admin"] = {"username": "admin", "password": password, "tables": sorted(resources)}
        replace_private_json(login_path, logins)
        replace_private_json(config_path, config)
        print("Created Admin local credentials; existing user passwords preserved.")
    configured = Settings.model_validate(config)
    all_resources = json.loads((ROOT / "analytics_service/config.local.json").read_text())["scopes"]["demo"]["resources"]
    report = verify_scope(configured.scopes["admin"], all_resources)
    report_path = ROOT / "analytics_service/admin-access-verification.local.json"
    if not report_path.exists():
        secure_json(report_path, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
