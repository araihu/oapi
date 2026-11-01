#!/usr/bin/env python3
"""Persist publication pins/history, then atomically deploy a verified static site."""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from publisher import Publisher, ROOT, digest, json_bytes, now, read_json, run, write_json

DATABASE_ID = "bd7765fc-78c0-4aac-865c-1fa04d7fb451"
ORIGIN = "https://oapi.araihu.com"
HISTORY_PATTERN = re.compile(
    r"(?:catalogs/[a-z][a-z0-9-]{0,24}/snapshots/snapshot-sha256-[0-9a-f]{64}/.+"
    r"|_manja/search/directory-sha256-[0-9a-f]{64}\.json"
    r"|brand-assets/[a-zA-Z0-9-]+-[0-9a-f]{12}\.[a-zA-Z0-9]+)\Z"
)


def archive_path(path):
    """Accept decoded export paths, never URL-decode filesystem names twice."""
    return bool(HISTORY_PATTERN.fullmatch(path)) and not any(
        part in {"", ".", ".."} for part in path.split("/")
    ) and "\\" not in path and not any(ord(c) < 32 or ord(c) == 127 for c in path)


class Cloudflare:
    def __init__(self):
        account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        self.token = os.environ["CLOUDFLARE_API_TOKEN"]
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{DATABASE_ID}/query"

    def query(self, queries):
        body = json_bytes({"batch": queries})
        for attempt in range(4):
            request = Request(self.url, data=body, headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            })
            try:
                with urlopen(request, timeout=90) as response:
                    result = json.load(response)
                if not result.get("success") or any(not r.get("success") for r in result["result"]):
                    raise RuntimeError("D1 query failed: " + json.dumps(result.get("errors", [])))
                return result["result"]
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    # Error responses contain API diagnostics, never request credentials.
                    raise RuntimeError(f"D1 HTTP {error.code}: {error.read().decode()[:2000]}") from None
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)

    def get(self, key):
        rows = self.query([{"sql": "SELECT value FROM metadata WHERE key=?", "params": [key]}])[0]["results"]
        return json.loads(rows[0]["value"]) if rows else None

    def put(self, key, value):
        self.query([{"sql": "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", "params": [key, json_bytes(value).decode()]}])

    def accept_pins(self, registry, expected):
        result = self.query([{
            "sql": "INSERT INTO metadata(key,value) VALUES('accepted',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE json_extract(metadata.value,'$.fingerprint') IS ?",
            "params": [json_bytes(registry).decode(), expected],
        }])[0]
        if result["meta"]["changes"] != 1:
            raise RuntimeError("Publication state changed concurrently; retry without deploying")


def archive_site(api, public):
    paths = sorted(p for p in public.rglob("*") if p.is_file() and archive_path(p.relative_to(public).as_posix()))
    batch = []
    archived = 0
    for offset in range(0, len(paths), 60):
        group = paths[offset:offset + 60]
        names = [p.relative_to(public).as_posix() for p in group]
        rows = api.query([{"sql": "SELECT path,sha256 FROM paths WHERE path IN (" + ",".join("?" for _ in names) + ")", "params": names}])[0]["results"]
        known = {row["path"]: row["sha256"] for row in rows}
        for file, path in zip(group, names):
            data = file.read_bytes()
            checksum = digest(data)
            if path in known:
                if known[path] != checksum:
                    raise ValueError(f"Immutable history differs: {path}")
                continue
            payload = base64.b64encode(gzip.compress(data, mtime=0)).decode()
            if len(payload) > 1_800_000:
                raise ValueError(f"History asset exceeds D1 row budget: {path}")
            media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            batch.extend([
                {"sql": "INSERT OR IGNORE INTO blobs(sha256,gzip_base64,content_type,byte_length) VALUES(?,?,?,?)", "params": [checksum, payload, media_type, len(data)]},
                {"sql": "INSERT INTO paths(path,sha256) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256", "params": [path, checksum]},
            ])
            archived += 1
            if len(batch) >= 30:
                api.query(batch)
                batch = []
    if batch:
        api.query(batch)
    print(json.dumps({"historyAssetsChecked": len(paths), "historyAssetsAdded": archived}), flush=True)


def restore_registry(publisher, registry):
    if registry is None:
        return
    restored = publisher.state / "restored"
    write_json(restored / "publications.json", registry)
    pointer = publisher.state / "current"
    if pointer.exists() and not pointer.is_symlink():
        raise ValueError("Expected a publisher-owned current symlink")
    pointer.unlink(missing_ok=True)
    pointer.symlink_to(restored)


def probe(api, *, force=False):
    publisher = Publisher(origin=ORIGIN)
    accepted, deployed = api.get("accepted"), api.get("deployed")
    refs = {}
    for project in publisher.config["projects"]:
        output = run(["git", "ls-remote", publisher.repository(project), project["developmentRef"], "refs/tags/openapi/*"], timeout=60)
        advertised = dict(line.split("\t", 1)[::-1] for line in output.splitlines())
        prefixes = tuple("refs/tags/openapi/" + spec["id"] + "/v" for spec in project["specs"])
        refs[project["id"]] = {ref: oid for ref, oid in advertised.items() if ref == project["developmentRef"] or (ref.startswith(prefixes) and not ref.endswith("^{}"))}
    inputs = dict(accepted.get("inputs", {})) if accepted else {}
    inputs.pop("renderer", None)
    changed = force or not accepted or not deployed or accepted["fingerprint"] != deployed["fingerprint"] or inputs != publisher.source_identity(refs)
    if not changed:
        api.put("health", {"status": "unchanged", "checkedAt": now(), "publishedAt": deployed["publishedAt"], "generation": deployed["generation"], "servingLastKnownGood": True})
    return changed


def prepare(api, *, force=False):
    publisher = Publisher(origin=ORIGIN)
    accepted = api.get("accepted")
    deployed = api.get("deployed")
    prior = dict(accepted) if accepted else None
    # A previous attempt may have persisted pins, then failed the atomic deploy.
    # Rebuild that same input rather than skipping it as already published.
    if prior and (force or not deployed or prior["fingerprint"] != deployed["fingerprint"]):
        prior.pop("fingerprint", None)
    restore_registry(publisher, prior)
    status = publisher.reconcile()
    if status["status"] == "unchanged":
        api.put("health", {**status, "servingLastKnownGood": True})
        return False
    generation = publisher.current()
    registry = publisher.registry()
    public = generation / "public"
    archive_site(api, public)
    # Once externally observable, tag identities must survive CI/redeploy failures.
    api.accept_pins(registry, accepted["fingerprint"] if accepted else None)
    write_json(ROOT / "aliases.json", registry["aliases"])
    # Keep Manja's verified export byte-for-byte intact. Hosting sidecars go in
    # a separate staging tree, never inside its closed export inventory.
    staging = publisher.state / "deploy"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(public, staging)
    public = staging
    # Public metadata is deliberately free of local bundle paths/credentials.
    write_json(public / "publications.json", registry)
    (public / "_headers").write_text(
        "/*\n  X-Content-Type-Options: nosniff\n"
        "/publications.json\n  Cache-Control: no-cache\n"
        "/catalogs/:catalog/snapshots/*\n  Cache-Control: public, max-age=31536000, immutable\n"
        "/_manja/search/directory-sha256-*\n  Cache-Control: public, max-age=31536000, immutable\n"
        "/brand-assets/*\n  Cache-Control: public, max-age=31536000, immutable\n"
    )
    write_json(publisher.state / "pending.json", {"registry": registry, "status": status})
    api.put("health", {"status": "publishing", "checkedAt": now(), "publishedAt": deployed.get("publishedAt") if deployed else None, "servingLastKnownGood": deployed is not None})
    return True


def accept(api):
    pending = read_json(ROOT / ".state/pending.json")
    registry = pending["registry"]
    if api.get("accepted")["fingerprint"] != registry["fingerprint"]:
        raise RuntimeError("Publication state changed before deployment acceptance")
    # Verify the real domain is already serving this exact generation.
    request = Request(ORIGIN + "/publications.json?deployment=" + registry["generation"], headers={"Cache-Control": "no-cache"})
    with urlopen(request, timeout=60) as response:
        remote = json.load(response)
    if remote["fingerprint"] != registry["fingerprint"]:
        raise RuntimeError("Public domain has not activated the expected generation")
    api.put("deployed", registry)
    api.put("health", {**pending["status"], "servingLastKnownGood": True})
    print(json.dumps(pending["status"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "prepare", "accept", "failure", "deploy"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    api = Cloudflare()
    try:
        if args.command == "failure":
            deployed = api.get("deployed")
            api.put("health", {"status": "error", "checkedAt": now(), "servingLastKnownGood": deployed is not None, "publishedAt": deployed.get("publishedAt") if deployed else None, "details": "Publication failed; see the publishing workflow in araihu/oapi."})
        elif args.command == "accept":
            accept(api)
        else:
            changed = probe(api, force=args.force) if args.command == "probe" else prepare(api, force=args.force)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write("changed=" + str(changed).lower() + "\n")
            if changed and args.command == "deploy":
                print(run([ROOT / "node_modules/.bin/wrangler", "deploy"], cwd=ROOT, timeout=180), flush=True)
                accept(api)
            elif not changed:
                print("No publication changes", flush=True)
    except Exception:
        # GitHub's failure step persists a diagnostic status while serving the
        # already deployed static files. Do not replace a successful generation.
        raise


if __name__ == "__main__":
    main()
