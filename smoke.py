#!/usr/bin/env python3
"""Check real production pages, source downloads and verified search assets."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from deploy import ORIGIN
from publisher import ROOT, digest, read_json


def fetch(path):
    request = Request(ORIGIN + quote(path, safe="/%"), headers={"Cache-Control": "no-cache"})
    for attempt in range(8):
        try:
            with urlopen(request, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"Unexpected HTTP {response.status}: {path}")
                return response.read()
        except (URLError, TimeoutError):
            if attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 15))


class SearchMetadata(HTMLParser):
    attributes = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id") == "catalog-search-dialog":
            self.attributes = attributes


def main():
    registry = json.loads(fetch("/publications.json"))
    expected = {p["id"] for p in read_json(ROOT / "catalogs.json")["projects"]}
    actual = {d["project"] for d in registry["documents"]}
    if actual != expected:
        raise RuntimeError(f"Project catalog mismatch: {actual} != {expected}")
    html = fetch("/").decode()
    metadata = SearchMetadata()
    metadata.feed(html)
    attributes = metadata.attributes
    directory_bytes = fetch(attributes["data-search-deployment-directory-url"])
    if digest(directory_bytes) != attributes["data-search-deployment-directory-sha256"]:
        raise RuntimeError("Global search directory digest mismatch")
    directory = json.loads(directory_bytes)
    paths = {f"/catalogs/{project}/" for project in expected}
    paths.update(doc["href"] for doc in registry["documents"])
    for catalog in directory["catalogs"]:
        child = fetch(catalog["childBase"] + catalog["directoryPath"])
        if digest(child) != catalog["directorySha256"]:
            raise RuntimeError("Catalog search directory digest mismatch")
    export = json.loads(fetch("/_manja/export.json"))
    # Verify source download bytes against the exact export inventory, including
    # internal APIs. No API server is contacted by these documentation checks.
    inventory = {item["path"]: item for item in export["files"]}
    sources = [path for path in inventory if "/source-files/" in path]

    def check_source(path):
        data = fetch("/" + path)
        item = inventory[path]
        if digest(data) != item["sha256"] or len(data) != item["length"]:
            raise RuntimeError("Source download digest mismatch: " + path)
        if not json.loads(data).get("openapi"):
            raise RuntimeError("Source download is not OpenAPI: " + path)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch, sorted(paths)))
        list(pool.map(check_source, sources))
    health = json.loads(fetch("/status.json"))
    if health.get("status") not in {"published", "unchanged", "publishing"}:
        raise RuntimeError("Publication is not healthy: " + json.dumps(health))
    print(json.dumps({"domain": ORIGIN, "generation": registry["generation"], "catalogs": len(expected), "documents": len(registry["documents"]), "sourceDownloadsVerified": len(sources), "globalSearchVerified": True}))


if __name__ == "__main__":
    main()
