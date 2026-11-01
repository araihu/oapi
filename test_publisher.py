import json
import http.server
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import time
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import demo
from publisher import Handler, Publisher, digest, immutable_public_path, run, semver_key, write_json


class SemverTests(unittest.TestCase):
    def test_numeric_and_prerelease_precedence(self):
        versions = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.9.0", "1.10.0", "2.0.0"]
        self.assertEqual(sorted(reversed(versions), key=semver_key), versions)
        for invalid in ["01.0.0", "1.0", "1.0.0-01", "v1.0.0"]:
            with self.assertRaises(ValueError):
                semver_key(invalid)


class PublicationTests(unittest.TestCase):
    def assert_old_search_assets_are_retained(self, publisher, old_generation):
        old_index = (old_generation / "public/_manja/search/directory.json").read_bytes()
        current_index = (publisher.current() / "public/_manja/search/directory.json").read_bytes()
        self.assertNotEqual(old_index, current_index)
        old_path = "/_manja/search/directory-sha256-" + digest(old_index) + ".json"
        catalog = json.loads(old_index)["catalogs"][0]
        old_child = catalog["childBase"] + catalog["directoryPath"]
        expected_child = (old_generation / "public" / old_child.lstrip("/")).read_bytes()
        (old_generation / "public/old-only.txt").write_text("must not be served from history")
        handler = type("RetentionTestHandler", (Handler,), {"publisher": publisher})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_port)
        try:
            for path, expected in [(old_path, old_index), (old_child, expected_child)]:
                with urlopen(origin + path) as response:
                    self.assertEqual(response.read(), expected)
                    self.assertIn("immutable", response.headers["Cache-Control"])
                with urlopen(Request(origin + path, method="HEAD")) as response:
                    self.assertEqual(int(response.headers["Content-Length"]), len(expected))
            with urlopen(origin + "/_manja/search/directory.json") as response:
                self.assertEqual(response.read(), current_index)
                self.assertEqual(response.headers["Cache-Control"], "no-cache")
            with self.assertRaises(HTTPError) as error:
                urlopen(origin + "/old-only.txt")
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_export_tracking_independent_releases_and_failed_updates(self):
        with tempfile.TemporaryDirectory(prefix="oapi-poc-test-") as temporary:
            root = Path(temporary)
            source = root / "upstream"
            demo.seed(source)
            config = {"schemaVersion": 1, "title": "PoC test", "projects": [{"id": "example", "title": "Example", "repository": str(source), "developmentRef": "refs/heads/main", "root": "api", "specs": [{"id": "public", "entrypoint": "public.yaml", "audience": "public"}, {"id": "internal", "entrypoint": "internal.yaml", "audience": "internal"}]}]}
            config["projects"][0]["specs"][1]["developmentName"] = "preview"
            write_json(root / "catalogs.json", config)
            publisher = Publisher(root / "catalogs.json", root / "state")
            self.assertEqual(publisher.reconcile()["status"], "published")
            first = publisher.registry()
            self.assertEqual(len(first["documents"]), 6)
            latest_public = "/catalogs/example/specs/public/latest/"
            latest_internal = "/catalogs/example/specs/internal/latest/"
            self.assertEqual(first["aliases"][latest_public], "/catalogs/example/documents/public-v1.1.0/")
            self.assertEqual(first["aliases"][latest_internal], "/catalogs/example/documents/internal-v0.1.0/")
            generation = publisher.current()
            public_page = (generation / "public/catalogs/example/documents/public-next/index.html").read_text()
            internal_page = (generation / "public/catalogs/example/documents/internal-next/index.html").read_text()
            self.assertIn("main · API", public_page)
            self.assertIn("preview · API", internal_page)
            self.assertNotIn("next · development", public_page)
            self.assertEqual(next(d for d in first["documents"] if d["key"] == "internal-next")["channelName"], "preview")
            self.assertEqual(publisher.reconcile()["status"], "unchanged")

            self.assertEqual(publisher.current(), generation)

            # Interrupted acquisition caches are replaceable; publication pins
            # and bundles live outside them and remain authoritative.
            cache = publisher.state / "git/example.git"
            (cache / "shallow.lock").write_text("interrupted Git operation\n")
            self.assertEqual(publisher.reconcile()["status"], "unchanged")
            self.assertFalse((cache / "shallow.lock").exists())
            self.assertEqual(publisher.registry()["releases"], first["releases"])
            self.assertTrue(list((publisher.state / "cache-recovery").glob("example.git-*")))

            # Only the public development channel changes; releases keep their bytes.
            demo.advance(source)
            self.assertEqual(publisher.reconcile()["status"], "published")
            second = publisher.registry()
            self.assertEqual(first["releases"], second["releases"])
            public_next = next(d for d in second["documents"] if d["key"] == "public-next")
            self.assertEqual(public_next["apiVersion"], "1.3.0-dev.2")
            self.assertNotEqual(generation, publisher.current())
            self.assert_old_search_assets_are_retained(publisher, generation)

            # An internal release advances its own alias, leaving public latest alone.
            demo.release_internal(source)
            publisher.reconcile()
            third = publisher.registry()
            self.assertEqual(third["aliases"][latest_public], first["aliases"][latest_public])
            self.assertEqual(third["aliases"][latest_internal], "/catalogs/example/documents/internal-v0.2.0/")
            good_generation = publisher.current()
            good_receipt = (good_generation / "public/_manja/export.json").read_bytes()

            # Invalid input cannot replace the verified publication.
            demo.break_source(source)
            with self.assertRaises(Exception):
                publisher.reconcile()
            self.assertEqual(publisher.current(), good_generation)
            self.assertEqual((good_generation / "public/_manja/export.json").read_bytes(), good_receipt)
            status = json.loads((publisher.state / "status.json").read_text())
            self.assertTrue(status["servingLastKnownGood"])
            demo.repair(source)
            publisher.reconcile()

            # A version tag must agree with info.version.
            demo.git(source, "tag", "openapi/public/v9.0.0")
            good_generation = publisher.current()
            with self.assertRaisesRegex(ValueError, "Tag/info.version mismatch"):
                publisher.reconcile()
            self.assertEqual(publisher.current(), good_generation)
            demo.git(source, "tag", "-d", "openapi/public/v9.0.0")

            # Both annotated and lightweight tag identities are frozen after publication.
            ref = "refs/tags/openapi/public/v1.1.0"
            old_object = demo.git(source, "rev-parse", ref)
            demo.git(source, "tag", "-f", "openapi/public/v1.1.0")
            with self.assertRaisesRegex(ValueError, "Immutable release tag moved"):
                publisher.reconcile()
            self.assertEqual(publisher.current(), good_generation)
            demo.git(source, "update-ref", ref, old_object)
            demo.git(source, "tag", "-d", "openapi/public/v1.1.0")
            with self.assertRaisesRegex(ValueError, "Published release tag disappeared"):
                publisher.reconcile()
            self.assertEqual(publisher.current(), good_generation)
            demo.git(source, "update-ref", ref, old_object)
            self.assertEqual(publisher.reconcile()["status"], "unchanged")

            # The fallback follows the configured Git ref, not a hardcoded label.
            prior_releases = publisher.registry()["releases"]
            demo.git(source, "branch", "master")
            config["projects"][0]["developmentRef"] = "refs/heads/master"
            write_json(root / "catalogs.json", config)
            renamed = Publisher(root / "catalogs.json", root / "state")
            self.assertEqual(renamed.reconcile()["status"], "published")
            renamed_page = (renamed.current() / "public/catalogs/example/documents/public-next/index.html").read_text()
            self.assertIn("master · API", renamed_page)
            self.assertEqual(renamed.registry()["releases"], prior_releases)


class ProcessTests(unittest.TestCase):
    def test_history_is_restricted_to_immutable_public_paths(self):
        prefix = "/catalogs/example/snapshots/snapshot-sha256-" + "a" * 64 + "/"
        self.assertIsNotNone(immutable_public_path(prefix + "search-data/search/directory.json"))
        for path in ["/_manja/search/directory.json", "/index.html", prefix + "../private", prefix + "%2e%2e/private", prefix + "x\\private"]:
            self.assertIsNone(immutable_public_path(path))

    @unittest.skipUnless(os.name == "posix", "publisher uses POSIX process groups")
    def test_timeout_stops_git_style_descendants(self):
        with tempfile.TemporaryDirectory(prefix="oapi-process-test-") as temporary:
            pid_path = Path(temporary) / "child.pid"
            child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            parent = "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                run([sys.executable, "-c", parent, pid_path, child], timeout=0.3)
            pid = int(pid_path.read_text())
            stat = Path(f"/proc/{pid}/stat")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if stat.read_text().split()[2] == "Z":
                        break
                except (FileNotFoundError, ProcessLookupError):
                    break
                time.sleep(0.01)
            else:
                self.fail("timed-out child remains alive")


if __name__ == "__main__":
    unittest.main()
