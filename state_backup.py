#!/usr/bin/env python3
"""Durable Git mirror of accepted pins; periodic commits keep GitHub cron active."""
import base64
import json
import os
from datetime import datetime, timezone

from deploy import Cloudflare
from publisher import json_bytes, now, run


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = "publication-state"
    path = f"repos/{repo}/contents/publications.json"
    api = Cloudflare()
    deployed = api.get("deployed")
    if not deployed:
        return
    reference = json.loads(run(["gh", "api", f"repos/{repo}/branches/main"]))
    # Creating a missing branch is safe to repeat; an existing branch is never reset.
    try:
        run(["gh", "api", f"repos/{repo}/git/ref/heads/{branch}"])
    except RuntimeError:
        run(["gh", "api", "--method", "POST", f"repos/{repo}/git/refs", "-f", "ref=refs/heads/" + branch, "-f", "sha=" + reference["commit"]["sha"]])
    existing = None
    try:
        existing = json.loads(run(["gh", "api", path + "?ref=" + branch]))
    except RuntimeError:
        pass
    if existing:
        prior = json.loads(base64.b64decode(existing["content"]))
        last_backup = datetime.fromisoformat(prior["checkedAt"])
        if prior["deployed"]["fingerprint"] == deployed["fingerprint"] and (datetime.now(timezone.utc) - last_backup).days < 25:
            print("Publication-state backup is current")
            return
    payload = {"branch": branch, "message": "Record published OpenAPI refs and publication health", "content": base64.b64encode(json_bytes({"checkedAt": now(), "accepted": api.get("accepted"), "deployed": deployed})).decode()}
    if existing:
        payload["sha"] = existing["sha"]
    # No token or spec data is interpolated into shell commands.
    from pathlib import Path
    target = Path(".state/github-state-backup.json")
    target.write_bytes(json_bytes(payload))
    run(["gh", "api", "--method", "PUT", path, "--input", target])
    print("Publication-state backup updated")


if __name__ == "__main__":
    main()
