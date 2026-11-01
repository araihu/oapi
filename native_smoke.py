#!/usr/bin/env python3
"""Run the real Worker, assets router and D1 engine against isolated local data."""
import base64
import gzip
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from publisher import ROOT, digest, run, write_json


def main():
    with tempfile.TemporaryDirectory(prefix="oapi-native-history-") as temporary:
        root = Path(temporary)
        public = root / "public"
        public.mkdir()
        (public / "index.html").write_text("<!doctype html><title>Native fixture</title>current assets")
        shutil.copyfile(ROOT / "worker.js", root / "worker.js")
        shutil.copyfile(ROOT / "reconciliation.js", root / "reconciliation.js")
        branch = "/catalogs/example/specs/public/branches/main/"
        release = "/catalogs/example/specs/public/versions/1.0.0/"
        write_json(root / "aliases.json", {branch: "/catalogs/example/documents/public-next/", release: "/catalogs/example/documents/public-v1.0.0/"})
        production_config = json.loads((ROOT / "wrangler.jsonc").read_text())
        config = {"name": "oapi-history-test", "main": "worker.js", "compatibility_date": production_config["compatibility_date"], "assets": {"directory": "public", "binding": "ASSETS", "html_handling": "auto-trailing-slash", "not_found_handling": "none"}, "d1_databases": [{"binding": "HISTORY", "database_name": "oapi-history-test", "database_id": "00000000-0000-0000-0000-000000000001"}]}
        write_json(root / "wrangler.json", config)
        content = '{"historical":"publicação imutável"}\n'.encode()
        checksum = digest(content)
        path = "catalogs/example/snapshots/snapshot-sha256-" + digest(b"fixture") + "/source-files/old %20?#é.json"
        payload = base64.b64encode(gzip.compress(content, mtime=0)).decode()
        sql = (ROOT / "schema.sql").read_text() + f"\nINSERT INTO blobs VALUES('{checksum}','{payload}','application/json',{len(content)});\nINSERT INTO paths VALUES('{path}','{checksum}');\nINSERT INTO metadata VALUES('health','{{\"status\":\"unchanged\"}}');\n"
        (root / "fixture.sql").write_text(sql)
        cli = ROOT / "node_modules/.bin/wrangler"
        args = ["--config", root / "wrangler.json", "--persist-to", root / "state"]
        run([cli, "d1", "execute", "oapi-history-test", "--local", *args, "--file", root / "fixture.sql"], cwd=ROOT)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"

        def fetch(path, method="GET", headers=None):
            request = Request(origin + path, method=method, headers=headers or {})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, response.headers, response.read()
            except HTTPError as error:
                return error.code, error.headers, error.read()

        with (root / "runtime.log").open("w+") as log:
            process = subprocess.Popen([str(x) for x in [cli, "dev", "--local", *args, "--port", port, "--ip", "127.0.0.1"]], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env={**os.environ, "WRANGLER_SEND_METRICS": "false"})
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        status, _, data = fetch("/")
                        assert status == 200 and b"current assets" in data
                        break
                    except (URLError, TimeoutError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise RuntimeError("Native Worker failed to start:\n" + log.read()[-6000:])
                        time.sleep(0.1)
                url = "/" + quote(path, safe="/")
                status, headers, data = fetch(url)
                assert status == 200 and data == content
                assert headers["ETag"] == '"' + checksum + '"'
                assert "immutable" in headers["Cache-Control"]
                status, headers, data = fetch(url, "HEAD")
                assert status == 200 and data == b"" and headers["Content-Length"] == str(len(content))
                assert fetch(url, headers={"If-None-Match": '"' + checksum + '"'})[0] == 304
                # Bypass urllib's automatic redirect following to inspect aliases.
                from urllib.request import HTTPRedirectHandler, build_opener
                class NoRedirect(HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None
                for alias, expected in [(branch, 307), (release, 308)]:
                    try:
                        build_opener(NoRedirect).open(origin + alias, timeout=5)
                        raise AssertionError("Expected alias redirect")
                    except HTTPError as response:
                        assert response.code == expected
                        assert response.headers["Location"].startswith("/catalogs/example/documents/")
                assert fetch("/private.json")[0] == 404
                assert fetch(url, "POST")[0] == 405
                assert json.loads(fetch("/status.json")[2])["status"] == "unchanged"
                assert json.loads(fetch("/reconciliation.json")[2])["status"] == "not-run"
                # A real scheduled invocation must persist failure safely when credentials are absent.
                status, _, _ = fetch("/cdn-cgi/local/scheduled?cron=%2A%2F5+%2A+%2A+%2A+%2A")
                assert status == 500
                reconciliation = json.loads(fetch("/reconciliation.json")[2])
                assert reconciliation["status"] == "failed"
                assert reconciliation["source"] == "cloudflare-cron"
                assert "GitHub App installation" in reconciliation["error"]
                assert fetch("/reconciliation.json", "POST")[0] == 405
                print("Native Worker + D1 passed: assets, history, aliases, method controls, health and persisted scheduled failure")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


if __name__ == "__main__":
    main()
