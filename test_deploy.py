import base64
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from io import BytesIO
from urllib.error import URLError
from unittest.mock import patch

import demo
import deploy
from publisher import Publisher, digest, write_json


class SQLiteCloudflare(deploy.Cloudflare):
    """Exercise production SQL, transactions and metadata CAS without API secrets."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript((deploy.ROOT / "schema.sql").read_text())

    def query(self, queries):
        results = []
        with self.db:
            for query in queries:
                before = self.db.total_changes
                cursor = self.db.execute(query["sql"], query.get("params", []))
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append({"success": True, "results": rows, "meta": {"changes": self.db.total_changes - before}})
        return results

    def previous_versions(self):
        return [{"version_id": "prior-worker", "percentage": 100}]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.api = SQLiteCloudflare()
        self.addCleanup(self.api.db.close)

    def test_archive_retains_exact_bytes_across_deployments_and_rejects_rewrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = "catalogs/example/snapshots/snapshot-sha256-" + "a" * 64 + "/source-files/a %20?#é.json"
            file = root / relative
            file.parent.mkdir(parents=True)
            data = b'{"version":"1.0.0"}\n'
            file.write_bytes(data)
            (root / "private.txt").write_text("not public history")
            deploy.archive_site(self.api, root)
            deploy.archive_site(self.api, root)
            row = self.api.query([{"sql": "SELECT * FROM paths JOIN blobs USING(sha256)"}])[0]["results"]
            self.assertEqual(len(row), 1)
            self.assertEqual(row[0]["path"], relative)
            self.assertEqual(gzip.decompress(base64.b64decode(row[0]["gzip_base64"])), data)
            self.assertEqual(row[0]["sha256"], digest(data))
            file.write_text("a different export must never reuse an immutable path")
            with self.assertRaisesRegex(ValueError, "Immutable history differs"):
                deploy.archive_site(self.api, root)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Immutable history path changed"):
                self.api.query([{"sql": "UPDATE paths SET sha256='wrong' WHERE path=?", "params": [relative]}])
            self.assertEqual(self.api.db.execute("SELECT sha256 FROM paths").fetchone()[0], digest(data))

    def test_acceptance_compares_prior_pins_before_a_new_deploy(self):
        first = {"fingerprint": "first", "releases": {"tag": "fixed"}}
        second = {"fingerprint": "second", "releases": {"tag": "fixed"}}
        self.api.accept_pins(first, None)
        self.api.accept_pins(second, "first")
        with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
            self.api.accept_pins({"fingerprint": "racing"}, "first")
        self.assertEqual(self.api.get("accepted"), second)

    def test_public_acceptance_waits_for_dns_then_records_the_verified_generation(self):
        registry = {"fingerprint": "fixed", "generation": "build-1", "publishedAt": "2026-11-01T00:00:00+00:00"}
        self.api.accept_pins(registry, None)
        pending = {"registry": registry, "status": {"status": "published"}}
        responses = [URLError("DNS propagation"), BytesIO(json.dumps(registry).encode())]
        with patch.object(deploy, "read_json", return_value=pending), patch.object(deploy, "urlopen", side_effect=responses), patch.object(deploy.time, "sleep"):
            deploy.accept(self.api)
        self.assertEqual(self.api.get("deployed"), registry)

    def test_failed_post_deploy_check_restores_previous_worker_but_retains_accepted_tag_pins(self):
        previous = {"fingerprint": "previous", "publishedAt": "2026-10-31T00:00:00+00:00"}
        accepted = {"fingerprint": "new", "releases": {"new-tag": "immutable"}}
        self.api.accept_pins(accepted, None)
        self.api.put("deployed", accepted)
        pending = {"previousRegistry": previous, "previousVersions": self.api.previous_versions()}
        with patch.object(deploy, "read_json", return_value=pending), patch.dict(deploy.os.environ, {"WORKER_DEPLOY_OUTCOME": "success"}), patch.object(self.api, "worker_request") as rollback:
            deploy.failure(self.api)
        self.assertEqual(rollback.call_args.args[0]["versions"], self.api.previous_versions())
        self.assertEqual(self.api.get("deployed"), previous)
        self.assertEqual(self.api.get("accepted"), accepted)
        self.assertTrue(self.api.get("health")["rolledBack"])

    def test_history_allows_only_immutable_canonical_paths(self):
        prefix = "catalogs/example/snapshots/snapshot-sha256-" + "a" * 64 + "/"
        self.assertTrue(deploy.archive_path(prefix + "source-files/a%20b.json"))
        self.assertTrue(deploy.archive_path("brand-assets/araihu-" + "b" * 12 + ".svg"))
        for value in ["private.txt", prefix + "../secret", prefix + "x\\secret", prefix + "a\x00b", "_manja/search/directory.json"]:
            self.assertFalse(deploy.archive_path(value))


class ProductionFlowTests(unittest.TestCase):
    def test_fresh_runner_restores_release_pins_retries_failed_deploy_and_skips_unchanged_refs(self):
        api = SQLiteCloudflare()
        self.addCleanup(api.db.close)
        with tempfile.TemporaryDirectory(prefix="oapi-production-test-") as temporary:
            root = Path(temporary)
            upstream = root / "upstream"
            demo.seed(upstream)
            config = root / "catalogs.json"
            write_json(config, {"schemaVersion": 1, "title": "Production test", "projects": [{"id": "example", "title": "Example", "repository": str(upstream), "developmentRef": "refs/heads/main", "root": "api", "specs": [{"id": "public", "entrypoint": "public.yaml", "audience": "public"}, {"id": "internal", "entrypoint": "internal.yaml", "audience": "internal"}]}]})
            make_publisher = lambda **kwargs: Publisher(config, root / "state", **kwargs)
            with patch.object(deploy, "Publisher", make_publisher), patch.object(deploy, "ROOT", root):
                self.assertTrue(deploy.probe(api))
                self.assertTrue(deploy.prepare(api))
                accepted = api.get("accepted")
                self.assertIsNone(api.get("deployed"))
                # The original export still passes Manja's exact inventory check.
                publisher = make_publisher(origin=deploy.ORIGIN)
                from publisher import run
                run([publisher.binary, "export", "verify", "--output", publisher.current() / "public"])
                self.assertIn("/catalogs/example/specs/public/branches/main/", accepted["aliases"])
                self.assertTrue(deploy.probe(api))
                # Accepted-but-not-deployed inputs must retry after a CI failure.
                self.assertTrue(deploy.prepare(api))
                accepted = api.get("accepted")
                api.put("deployed", accepted)
                self.assertFalse(deploy.probe(api))
                self.assertEqual(api.get("health")["status"], "unchanged")
                demo.advance(upstream)
                self.assertTrue(deploy.probe(api))
                self.assertTrue(deploy.prepare(api))
                self.assertEqual(api.get("accepted")["releases"], accepted["releases"])
                # A moved published tag is rejected using durable pins restored
                # from the API, regardless of ephemeral runner state.
                demo.git(upstream, "tag", "-f", "openapi/public/v1.1.0")
                with self.assertRaisesRegex(ValueError, "Immutable release tag moved"):
                    deploy.prepare(api)
                self.assertEqual(api.get("deployed"), accepted)


if __name__ == "__main__":
    unittest.main()
