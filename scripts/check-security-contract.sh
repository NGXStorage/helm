#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export ROOT_DIR

python3 - <<'PY'
import os
import pathlib
import subprocess
import sys

import yaml

root = pathlib.Path(os.environ["ROOT_DIR"])
sys.path.insert(0, str(root / "scripts"))
from security_contract import ContractError, validate_docs

for protocol in ("fc", "iscsi", "nfs"):
    for platform in ("kubernetes", "openshift"):
        rendered = subprocess.check_output(
            ["helm", "template", "security-contract", str(root / protocol / platform), "--namespace", "ngxstorage"],
            text=True,
        )
        docs = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
        try:
            validate_docs(docs, protocol, platform)
        except ContractError as error:
            raise SystemExit(f"security contract failed: {error}") from error
        print(f"{protocol}/{platform}: security contract passed")
PY
