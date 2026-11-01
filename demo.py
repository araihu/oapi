#!/usr/bin/env python3
"""Controlled source repository for the local publishing demonstration."""
import argparse
from pathlib import Path
import re
import subprocess

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = ROOT / ".state/demo-upstream"


def git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.STDOUT, text=True).strip()


def write(repository, name, document):
    path = repository / "api" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def commit(repository, message):
    git(repository, "add", "api")
    git(repository, "commit", "--quiet", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def operation(operation_id, summary):
    return {"get": {"operationId": operation_id, "summary": summary, "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}}}}}


def seed(repository):
    if (repository / ".git").exists():
        return git(repository, "rev-parse", "HEAD")
    repository.mkdir(parents=True, exist_ok=True)
    git(repository, "init", "--quiet", "-b", "main")
    git(repository, "config", "user.name", "Araihu Local PoC")
    git(repository, "config", "user.email", "poc@example.test")
    public = {"openapi": "3.0.3", "info": {"title": "PoC Public API", "version": "1.0.0", "description": "Contrato fictício para demonstrar tags imutáveis e acompanhamento de main."}, "paths": {"/widgets": operation("listWidgets", "List widgets")}, "components": {"schemas": {"Widget": {"$ref": "./components.yaml#/Widget"}}}}
    internal = {"openapi": "3.0.3", "info": {"title": "PoC Internal API", "version": "0.1.0", "description": "Contrato administrativo fictício, versionado independentemente da API pública."}, "paths": {"/internal/health": operation("internalHealth", "Check internal health")}}
    write(repository, "public.yaml", public)
    write(repository, "internal.yaml", internal)
    write(repository, "components.yaml", {"Widget": {"type": "object", "properties": {"id": {"type": "string"}}}})
    commit(repository, "Initial public and internal contracts")
    git(repository, "tag", "openapi/public/v1.0.0")
    git(repository, "tag", "openapi/internal/v0.1.0")
    public["info"]["version"] = "1.1.0"
    public["paths"]["/widgets/count"] = operation("countWidgets", "Count widgets")
    write(repository, "public.yaml", public)
    commit(repository, "Public 1.1.0; internal remains 0.1.0")
    git(repository, "tag", "-a", "openapi/public/v1.1.0", "-m", "Public contract 1.1.0")
    public["info"]["version"] = "1.2.0-rc.1"
    public["paths"]["/widgets/preview"] = operation("previewWidgets", "Preview prerelease functionality")
    write(repository, "public.yaml", public)
    commit(repository, "Public prerelease")
    git(repository, "tag", "openapi/public/v1.2.0-rc.1")
    public["info"]["version"] = "1.3.0-dev.1"
    write(repository, "public.yaml", public)
    return commit(repository, "Continue development on main")


def advance(repository):
    path = repository / "api/public.yaml"
    public = yaml.safe_load(path.read_text())
    match = re.fullmatch(r"1\.3\.0-dev\.(\d+)", public["info"]["version"])
    iteration = int(match.group(1)) + 1 if match else 2
    public["info"]["version"] = f"1.3.0-dev.{iteration}"
    public["paths"][f"/widgets/update-{iteration}"] = operation(f"updateIteration{iteration}", f"Automatically published iteration {iteration}")
    write(repository, "public.yaml", public)
    return commit(repository, f"Advance public development to iteration {iteration}")


def release_internal(repository):
    internal = yaml.safe_load((repository / "api/internal.yaml").read_text())
    major, minor, patch = map(int, internal["info"]["version"].split("."))
    version = f"{major}.{minor + 1}.0"
    internal["info"]["version"] = version
    internal["paths"][f"/internal/audit-{minor + 1}"] = operation(f"internalAudit{minor + 1}", "Inspect internal audit state")
    write(repository, "internal.yaml", internal)
    revision = commit(repository, f"Release internal {version} independently")
    git(repository, "tag", f"openapi/internal/v{version}")
    return revision


def break_source(repository):
    path = repository / "api/public.yaml"
    backup = repository / ".git/poc-valid-public.yaml"
    if backup.exists():
        raise ValueError("Demo already broken; run repair first")
    backup.write_bytes(path.read_bytes())
    path.write_text("openapi: 3.0.3\ninfo: [invalid YAML\n")
    return commit(repository, "Deliberately invalid spec to demonstrate last-known-good")


def repair(repository):
    backup = repository / ".git/poc-valid-public.yaml"
    (repository / "api/public.yaml").write_bytes(backup.read_bytes())
    backup.unlink()
    return commit(repository, "Repair deliberately invalid demo spec")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "advance", "release-internal", "break", "repair"])
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    args = parser.parse_args()
    functions = {"seed": seed, "advance": advance, "release-internal": release_internal, "break": break_source, "repair": repair}
    print(functions[args.command](args.repository.resolve()))
