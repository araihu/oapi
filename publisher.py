#!/usr/bin/env python3
"""Git -> immutable bundles -> verified Manja exports."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parent
MANJA_REVISION = "56318e36ec4d1afff6bf4592e38645ed75df78be"
MANJA_EXPORT_TIMEOUT_SECONDS = 600
REDOCLY_VERSION = "2.65.0"
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,24}\Z")
PINNED_SEARCH = re.compile(r"_manja/search/directory-sha256-([0-9a-f]{64})\.json\Z")


def immutable_public_path(path):
    decoded = unquote(path)
    if not decoded.startswith("/") or "\\" in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    parts = decoded[1:].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    relative = "/".join(parts)
    if PINNED_SEARCH.fullmatch(relative) or re.match(r"catalogs/[a-z][a-z0-9-]{0,24}/snapshots/snapshot-sha256-[0-9a-f]{64}/", relative):
        return Path(relative)
    return None


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as output:
        output.write(json_bytes(value))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run(args, *, cwd=None, binary=False, timeout=180):
    process = subprocess.Popen([str(a) for a in args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # A Git timeout must also stop upload-pack/HTTPS/SSH children before
        # another reconciliation can use or recover this publisher-owned cache.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The parent can exit before a child which ignores SIGTERM.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise
    if process.returncode:
        diagnostic = stderr.decode(errors="replace")[-5000:]
        raise RuntimeError(f"{Path(str(args[0])).name}: {diagnostic.strip()}")
    return stdout if binary else stdout.decode().strip()


def git(repository, *args, binary=False):
    attempts = 3 if args and args[0] == "fetch" else 1
    # Explicit blob acquisition in a partial clone must not advertise commits
    # as proof that all their reachable blobs are already present.
    options = ["-c", "fetch.negotiationAlgorithm=noop"] if attempts > 1 else []
    for attempt in range(attempts):
        try:
            return run(["git", *options, "-C", repository, *args], binary=binary, timeout=60)
        except RuntimeError:
            if attempt + 1 == attempts:
                raise
            time.sleep(attempt + 1)


def semver_key(version):
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"Invalid SemVer (build metadata not supported in PoC): {version}")
    major, minor, patch, prerelease = match.groups()
    identifiers = []
    for part in prerelease.split(".") if prerelease else []:
        if part.isdigit() and len(part) > 1 and part.startswith("0"):
            raise ValueError(f"Leading zero in prerelease: {version}")
        identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    return int(major), int(minor), int(patch), (1, ()) if prerelease is None else (0, tuple(identifiers))


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) != value or "\\" in value:
        raise ValueError(f"Unsafe source path: {value}")
    return path


def check_refs(value, base, source_root):
    """The bundle may only read captured, repository-owned YAML/JSON files."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str) and not child.startswith("#"):
                parsed = urlsplit(child)
                if parsed.scheme or parsed.netloc or parsed.query:
                    raise ValueError(f"External $ref is outside this PoC's source boundary: {child}")
                target = (base / unquote(parsed.path)).resolve()
                if not target.is_relative_to(source_root.resolve()) or not target.is_file():
                    raise ValueError(f"$ref escapes source root or is missing: {child}")
            check_refs(child, base, source_root)
    elif isinstance(value, list):
        for child in value:
            check_refs(child, base, source_root)


class Publisher:
    def __init__(self, config=ROOT / "catalogs.json", state=ROOT / ".state", origin="http://127.0.0.1:8787"):
        self.config_path = Path(config).resolve()
        self.config = read_json(self.config_path)
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.origin = origin.rstrip("/")
        self.binary = Path(os.environ.get("MANJA_BIN", ROOT / ".tools/manja")).resolve()
        self.redocly = ROOT / "node_modules/.bin/redocly"
        if self.config.get("schemaVersion") != 1 or not self.config.get("projects"):
            raise ValueError("Expected schemaVersion 1 and a nonempty project list")
        seen = set()
        for project in self.config["projects"]:
            if not SLUG.fullmatch(project["id"]) or project["id"] in seen:
                raise ValueError("Project IDs must be unique lowercase slugs")
            seen.add(project["id"])
            safe_relative(project["root"])
            if not project["developmentRef"].startswith("refs/heads/"):
                raise ValueError("developmentRef must be a full branch ref")
            self.validate_development_name(project.get("developmentName"))
            names = set()
            for spec in project["specs"]:
                if not SLUG.fullmatch(spec["id"]) or spec["id"] in names:
                    raise ValueError("Spec IDs must be unique lowercase slugs")
                names.add(spec["id"])
                safe_relative(spec["entrypoint"])
                self.validate_development_name(spec.get("developmentName"))

    @staticmethod
    def validate_development_name(name):
        if name is not None and (not isinstance(name, str) or not name or name != name.strip() or len(name.encode("utf-8")) > 128 or not name.isprintable()):
            raise ValueError("developmentName must be nonempty printable text of at most 128 bytes")

    def current(self):
        pointer = self.state / "current"
        return pointer.resolve() if pointer.is_symlink() else None

    def registry(self):
        current = self.current()
        return read_json(current / "publications.json") if current else {"releases": {}, "documents": [], "aliases": {}}

    def retained_public_file(self, relative):
        """Resolve only immutable public assets from completed, verified exports."""
        if immutable_public_path("/" + relative.as_posix()) != relative:
            return None
        current = self.current()
        generations = sorted(self._generations().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = ([current] if current else []) + [g for g in generations if g != current]
        pinned = PINNED_SEARCH.fullmatch(relative.as_posix())
        for generation in candidates:
            if generation.is_symlink() or not (generation / "publications.json").is_file() or not (generation / "verify-receipt.json").is_file():
                continue
            public = (generation / "public").resolve()
            candidate = (public / relative).resolve()
            if candidate.is_relative_to(public) and candidate.is_file():
                return candidate
            # Exports predating pinned directory URLs still carry the exact
            # directory bytes. Expose those only under their verified digest.
            if pinned:
                legacy = public / "_manja/search/directory.json"
                if legacy.is_file() and legacy.resolve().is_relative_to(public) and digest(legacy.read_bytes()) == pinned.group(1):
                    return legacy
        return None

    def repository(self, project):
        repository = project["repository"]
        if repository.startswith("."):
            return str((self.config_path.parent / repository).resolve())
        return repository

    def observe(self, project, prior):
        repository = self.repository(project)
        cache = self.state / "git" / (project["id"] + ".git")
        if cache.exists():
            if git(cache, "remote", "get-url", "origin") != repository:
                raise ValueError(f"Repository identity changed for {project['id']}; use a fresh state directory")
            # The outer reconciliation lock owns all operations on .state/git.
            # Preserve interrupted caches for diagnosis instead of deleting
            # individual lock files or touching the source repository.
            if any(cache.rglob("*.lock")):
                recovery = self.state / "cache-recovery"
                recovery.mkdir(exist_ok=True)
                os.replace(cache, recovery / (cache.name + "-" + uuid.uuid4().hex))
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "init", "--bare", "--quiet", cache])
            git(cache, "remote", "add", "origin", repository)
        output = run(["git", "ls-remote", repository, project["developmentRef"], "refs/tags/openapi/*"], timeout=60)
        advertised = dict(line.split("\t", 1)[::-1] for line in output.splitlines())
        development_ref = project["developmentRef"]
        if development_ref not in advertised:
            raise ValueError(f"Missing development ref: {project['id']} {development_ref}")
        selected = {development_ref: advertised[development_ref]}
        releases = []
        for spec in project["specs"]:
            prefix = f"refs/tags/openapi/{spec['id']}/v"
            for ref, object_id in advertised.items():
                if not ref.startswith(prefix) or ref.endswith("^{}"):
                    continue
                version = ref[len(prefix):]
                semver_key(version)
                selected[ref] = object_id
                release_id = f"{project['id']}:{ref}"
                previous = prior["releases"].get(release_id)
                if previous and previous["tagObject"] != object_id:
                    raise ValueError(f"Immutable release tag moved: {release_id}")
                releases.append({"spec": spec["id"], "version": version, "ref": ref, "tagObject": object_id, "releaseId": release_id})
        for release_id, previous in prior["releases"].items():
            if release_id.startswith(project["id"] + ":") and previous["ref"] not in selected:
                raise ValueError(f"Published release tag disappeared: {release_id}")
        # Every read below uses a verified commit, never a mutable working tree.
        cached = {}
        for ref in selected:
            try:
                cached[ref] = git(cache, "rev-parse", "--verify", ref)
            except RuntimeError:
                break
        if cached != selected:
            git(cache, "fetch", "--quiet", "--depth=1", "--filter=blob:none", "--no-tags", "origin", *[f"+{ref}:{ref}" for ref in selected])
        commits = {}
        for ref, expected in selected.items():
            if git(cache, "rev-parse", "--verify", ref) != expected:
                raise ValueError(f"Ref changed during acquisition; retry: {ref}")
            commits[ref] = git(cache, "rev-parse", "--verify", ref + "^{commit}")
        return {"project": project, "cache": cache, "objects": selected, "commits": commits, "releases": releases}

    def capture(self, observed, commit):
        project, repository = observed["project"], observed["cache"]
        destination = self.state / "captures" / project["id"] / commit / digest(project["root"].encode())[:12]
        if (destination / ".complete").exists():
            return destination
        destination.mkdir(parents=True, exist_ok=True)
        inventory = git(repository, "ls-tree", "-rz", commit, "--", project["root"], binary=True)
        selected = []
        for record in inventory.split(b"\0"):
            if not record:
                continue
            metadata, filename = record.split(b"\t", 1)
            mode, kind, object_id = metadata.decode().split()
            relative = PurePosixPath(filename.decode()).relative_to(project["root"])
            if mode == "120000":
                raise ValueError(f"Symlink inside declared API source root: {filename.decode()}")
            if kind != "blob" or relative.suffix.lower() not in {".yaml", ".yml", ".json"}:
                continue
            selected.append((relative, object_id))
        # Acquire the selected blobs together. One lazy network fetch per YAML
        # file is slow and fragile for exploded contracts such as Moco's.
        if selected:
            git(repository, "fetch", "--quiet", "--filter=blob:none", "--no-tags", "--no-write-fetch-head", "origin", *sorted({oid for _, oid in selected}))
        for relative, object_id in selected:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(repository, "cat-file", "blob", object_id, binary=True))
        for filename in destination.rglob("*"):
            if filename.suffix in {".yaml", ".yml", ".json"}:
                check_refs(yaml.safe_load(filename.read_text()), filename.parent, destination)
        (destination / ".complete").touch()
        return destination

    def bundle(self, observed, spec, ref, version=None):
        project = observed["project"]
        commit = observed["commits"][ref]
        captured = self.capture(observed, commit)
        entrypoint = captured / spec["entrypoint"]
        bundle_key = digest(json_bytes([project["id"], spec["id"], commit, project["root"], spec["entrypoint"], REDOCLY_VERSION]))
        target = self.state / "bundles" / (bundle_key + ".json")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            run([self.redocly, "bundle", entrypoint, "--output", target, "--ext", "json"])
        content = target.read_bytes()
        document = json.loads(content)
        if not str(document.get("openapi", "")).startswith("3.") or not isinstance(document.get("paths"), dict):
            raise ValueError(f"Not an OpenAPI 3 document: {project['id']}/{spec['id']}")
        api_version = document.get("info", {}).get("version")
        if version is not None and api_version != version:
            raise ValueError(f"Tag/info.version mismatch for {ref}: expected {version}, got {api_version}")
        # Stable keys also fit Manja's current flat document model.
        key = spec["id"] + ("-v" + version.lower() if version else "-next")
        if version and version != version.lower():
            key += "-" + digest(version.encode())[:8]
        if len(key) > 64:
            raise ValueError("Version creates a document key longer than Manja's 64-character limit")
        document = {"project": project["id"], "spec": spec["id"], "audience": spec["audience"], "channel": "release" if version else "branch", "version": version, "apiVersion": api_version, "key": key, "ref": ref, "commit": commit, "sha256": digest(content), "repository": self.repository(project), "sourcePath": str(PurePosixPath(project["root"]) / spec["entrypoint"]), "bundle": str(target), "href": f"/catalogs/{project['id']}/documents/{key}/"}
        name = spec.get("developmentName", project.get("developmentName"))
        if not version and name is not None:
            document["channelName"] = name
        return document

    def reconcile(self):
        with (self.state / "reconcile.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy"}
            attempted = now()
            try:
                return self._reconcile(attempted)
            except Exception as error:
                status = {"status": "error", "checkedAt": attempted, "error": str(error), "servingLastKnownGood": self.current() is not None}
                write_json(self.state / "status.json", status)
                raise

    def source_identity(self, refs, brand_hashes=None):
        if brand_hashes is None:
            names = [p["icon"] for p in self.config["projects"] if p.get("icon")]
            logo = self.config.get("branding", {}).get("logoFile")
            if logo:
                names.append(logo)
            brand_hashes = {name: digest((self.config_path.parent / name).read_bytes()) for name in names}
        return {"config": self.config, "refs": refs, "brandAssets": brand_hashes, "origin": self.origin, "redocly": REDOCLY_VERSION, "publisher": digest(Path(__file__).read_bytes()), "deployment": {name: digest((ROOT / name).read_bytes()) for name in ("deploy.py", "worker.js", "wrangler.jsonc", "package-lock.json", "setup.sh", "requirements.txt", ".github/workflows/publish.yml") if (ROOT / name).is_file()}}

    def _reconcile(self, attempted):
        if not self.binary.is_file() or not self.redocly.is_file():
            raise ValueError("Run setup.sh first (Manja and Redocly are required)")
        prior = self.registry()
        observed = [self.observe(project, prior) for project in self.config["projects"]]
        brand_files = [p["icon"] for p in self.config["projects"] if p.get("icon")]
        if self.config.get("branding", {}).get("logoFile"):
            brand_files.append(self.config["branding"]["logoFile"])
        brand_hashes = {name: digest((self.config_path.parent / name).read_bytes()) for name in brand_files}
        identity = {**self.source_identity({item["project"]["id"]: item["objects"] for item in observed}, brand_hashes), "renderer": digest(self.binary.read_bytes())}
        fingerprint = digest(json_bytes(identity))
        if prior.get("fingerprint") == fingerprint:
            status = {"status": "unchanged", "checkedAt": attempted, "publishedAt": prior["publishedAt"], "generation": prior["generation"]}
            write_json(self.state / "status.json", status)
            return status
        generation = Path(tempfile.mkdtemp(prefix="build-", dir=self._generations()))
        try:
            brand_root = generation / "brand"
            brand_urls = {}
            for name, checksum in brand_hashes.items():
                source = self.config_path.parent / name
                filename = source.stem + "-" + checksum[:12] + source.suffix
                brand_root.mkdir(exist_ok=True)
                shutil.copyfile(source, brand_root / filename)
                brand_urls[name] = "/brand-assets/" + filename
            registry = {"schemaVersion": 1, "inputs": identity, "fingerprint": fingerprint, "generation": generation.name, "publishedAt": now(), "manjaRevision": MANJA_REVISION, "releases": dict(prior["releases"]), "documents": [], "aliases": {}}
            catalogs = []
            for item in observed:
                project = item["project"]
                documents = []
                for spec in project["specs"]:
                    development = self.bundle(item, spec, project["developmentRef"])
                    documents.append(development)
                    prefix = f"/catalogs/{project['id']}/specs/{spec['id']}/"
                    registry["aliases"][prefix + "next/"] = development["href"]
                    branch = project["developmentRef"].removeprefix("refs/heads/")
                    registry["aliases"][prefix + "branches/" + quote(branch, safe="") + "/"] = development["href"]
                    releases = sorted([r for r in item["releases"] if r["spec"] == spec["id"]], key=lambda r: semver_key(r["version"]), reverse=True)
                    stable = []
                    for release in releases:
                        doc = self.bundle(item, spec, release["ref"], release["version"])
                        previous = prior["releases"].get(release["releaseId"])
                        if previous and previous["sha256"] != doc["sha256"]:
                            raise ValueError(f"Immutable release bundle changed: {release['releaseId']}")
                        documents.append(doc)
                        registry["releases"][release["releaseId"]] = {**release, "commit": doc["commit"], "sha256": doc["sha256"]}
                        registry["aliases"][prefix + "versions/" + release["version"] + "/"] = doc["href"]
                        if VERSION.fullmatch(release["version"]).group(4) is None:
                            stable.append(doc)
                    if stable:
                        registry["aliases"][prefix + "latest/"] = stable[0]["href"]
                root = generation / "specs" / project["id"]
                root.mkdir(parents=True)
                for doc in documents:
                    shutil.copyfile(doc["bundle"], root / (doc["key"] + ".json"))
                readme = self.catalog_readme(project, documents, registry["aliases"])
                default_spec = project["specs"][0]["id"]
                default_href = registry["aliases"].get(f"/catalogs/{project['id']}/specs/{default_spec}/latest/")
                default_document = next((d["key"] for d in documents if d["href"] == default_href), default_spec + "-next")
                catalog = {"id": project["id"], "mount": f"/catalogs/{project['id']}", "title": project["title"], "readme": readme, "defaultDocument": default_document, "profile": "strict-v1", "source": {"kind": "files", "root": str(root), "include": [d["key"] + ".json" for d in documents], "documents": [{"path": d["key"] + ".json", "key": d["key"]} for d in documents]}}
                for mapping, doc in zip(catalog["source"]["documents"], documents):
                    latest = registry["aliases"].get(f"/catalogs/{project['id']}/specs/{doc['spec']}/latest/") == doc["href"]
                    publication = {"api": doc["spec"], "audience": doc["audience"], "channel": doc["channel"], "version": doc["version"] or "", "latest": latest, "ref": doc["ref"], "commit": doc["commit"], "sourcePath": doc["sourcePath"]}
                    if "channelName" in doc:
                        publication["channelName"] = doc["channelName"]
                    if project["repository"].startswith("https://"):
                        publication["repositoryURL"] = project["repository"].removesuffix(".git")
                    mapping["publication"] = publication
                if "catalogOverview" in project:
                    catalog["catalogOverview"] = project["catalogOverview"]
                if project.get("icon"):
                    image = brand_urls[project["icon"]]
                    catalog["branding"] = {"displayName": project["title"], "logo": {"src": image, "alt": project["title"]}, "favicon": image}
                catalogs.append(catalog)
                registry["documents"].extend([{k: v for k, v in d.items() if k != "bundle"} for d in documents])
            config = {"version": 1, "organization": {"title": self.config["title"], "readme": self.config.get("description", "Referências OpenAPI dos projetos Araihu.")}, "catalogs": catalogs}
            if brand_files:
                config["assetDir"] = str(brand_root)
            organization_branding = self.config.get("branding", {})
            if organization_branding.get("logoFile"):
                image = brand_urls[organization_branding["logoFile"]]
                config["organization"]["branding"] = {"displayName": organization_branding["displayName"], "logo": {"src": image, "alt": organization_branding.get("logoAlt", ""), "homeUrl": "/"}, "favicon": image}
            # Manja requires HTTPS for SEO canonical origins; local HTTP has none.
            if self.origin.startswith("https://"):
                config["organization"]["seo"] = {"canonicalBase": self.origin}
            write_json(generation / "renderer.yaml", config)
            export = run(
                [self.binary, "export", "--renderer-config", generation / "renderer.yaml",
                 "--data-dir", generation / "data", "--output", generation / "public",
                 "--base-path", "/", "--fragment-workers", "4"],
                timeout=MANJA_EXPORT_TIMEOUT_SECONDS,
            )
            write_json(generation / "export-receipt.json", json.loads(export))
            verification = run([self.binary, "export", "verify", "--output", generation / "public"])
            write_json(generation / "verify-receipt.json", json.loads(verification))
            registry["publishedAt"] = now()
            write_json(generation / "publications.json", registry)
            status = {"status": "published", "checkedAt": attempted, "publishedAt": registry["publishedAt"], "generation": generation.name, "catalogs": len(catalogs), "documents": len(registry["documents"])}
            write_json(self.state / "status.json", status)
            temporary = self.state / "current.tmp"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(generation)
            os.replace(temporary, self.state / "current")
            sync_directory(self.state)
            return status
        except BaseException:
            if self.current() != generation:
                shutil.rmtree(generation)
            raise

    def _generations(self):
        directory = self.state / "generations"
        directory.mkdir(exist_ok=True)
        return directory

    def catalog_readme(self, project, documents, aliases):
        return ("Demonstração local de publicações independentes das APIs pública e interna. Estas tags pertencem apenas ao repositório Git de teste." if project.get("demo") else "Selecione uma API e sua publicação para consultar operações e schemas.")


class Handler(http.server.SimpleHTTPRequestHandler):
    publisher: Publisher

    def do_GET(self):
        self._serving_public = False
        self._public_immutable_file = False
        path = urlsplit(self.path).path
        if path in {"/status.json", "/publications.json"}:
            value = read_json(self.publisher.state / "status.json", {}) if path == "/status.json" else self.publisher.registry()
            data = json_bytes(value)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        target = self.publisher.registry()["aliases"].get(path)
        if target:
            self.send_response(307 if path.endswith(("/next/", "/latest/")) else 308)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        current = self.publisher.current()
        if not current:
            self.send_error(503, "No valid publication yet")
            return
        self.directory = str(current / "public")
        self._serving_public = True
        super().do_GET()

    def do_HEAD(self):
        self._serving_public = False
        self._public_immutable_file = False
        current = self.publisher.current()
        if not current:
            self.send_error(503)
            return
        self.directory = str(current / "public")
        self._serving_public = True
        super().do_HEAD()

    def translate_path(self, path):
        translated = Path(super().translate_path(path))
        relative = immutable_public_path(urlsplit(path).path)
        if relative is not None:
            if not translated.is_file():
                retained = self.publisher.retained_public_file(relative)
                if retained is not None:
                    translated = retained
            self._public_immutable_file = translated.is_file()
        return str(translated)

    def end_headers(self):
        if getattr(self, "_serving_public", False):
            policy = "public, max-age=31536000, immutable" if self._public_immutable_file else "no-cache"
            self.send_header("Cache-Control", policy)
        super().end_headers()

    def log_message(self, format, *args):
        if args and str(args[1] if len(args) > 1 else "") not in {"200", "304"}:
            super().log_message(format, *args)


def serve(publisher, host, port, interval):
    if not publisher.current():
        print(json.dumps(publisher.reconcile()), flush=True)
    else:
        print(f"Serving verified generation {publisher.current().name}; checking Git in background", flush=True)
    handler = type("LocalPublisherHandler", (Handler,), {"publisher": publisher})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    stopped = threading.Event()

    def watch():
        while not stopped.is_set():
            try:
                result = publisher.reconcile()
                if result["status"] != "unchanged":
                    print(json.dumps(result), flush=True)
            except Exception as error:
                print(f"Reconciliation failed; keeping current publication: {error}", flush=True)
            if stopped.wait(interval):
                break

    threading.Thread(target=watch, name="git-reconciler", daemon=True).start()
    print(f"PoC ready: http://{host}:{port} (Git polling every {interval:g}s)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["reconcile", "serve"])
    parser.add_argument("--config", type=Path, default=ROOT / "catalogs.json")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least one second")
    publisher = Publisher(args.config, args.state, f"http://{args.host}:{args.port}")
    if args.command == "serve":
        serve(publisher, args.host, args.port, args.interval)
    else:
        print(json.dumps(publisher.reconcile(), ensure_ascii=False))


if __name__ == "__main__":
    main()
