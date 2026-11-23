"""Check that each tracked chart index describes the same immutable packages."""

import hashlib
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def read_feed(directory):
    index = yaml.safe_load((directory / "index.yaml").read_text())
    packages = {path.name: path for path in directory.glob("*.tgz")}
    indexed = set()
    records = {}
    for name, versions in index["entries"].items():
        for entry in versions:
            key = (name, entry["version"])
            if entry["name"] != name or key in records or len(entry["urls"]) != 1:
                raise SystemExit(f"{directory}: invalid index entry {key}")
            url = entry["urls"][0]
            filename = Path(urlsplit(url).path).name
            if filename not in packages:
                raise SystemExit(f"{directory}: missing package {filename}")
            digest = hashlib.sha256(packages[filename].read_bytes()).hexdigest()
            if digest != entry["digest"]:
                raise SystemExit(f"{directory}: index digest mismatch for {filename}")
            indexed.add(filename)
            records[key] = (url, digest)
    if indexed != packages.keys():
        raise SystemExit(f"{directory}: unindexed packages: {sorted(packages.keys() - indexed)}")
    return records


packaged, published = map(read_feed, map(Path, sys.argv[1:]))
if packaged != published:
    differences = sorted(key for key in packaged.keys() | published.keys()
                         if packaged.get(key) != published.get(key))
    raise SystemExit(f"Repo and Pages index entries differ: {differences}")
print(f"Repo and Pages match for {len(packaged)} chart versions")
