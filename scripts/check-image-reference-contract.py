#!/usr/bin/env python3
"""Validate tag fallback and immutable digest rendering for all CSI charts."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOLS = ("fc", "iscsi", "nfs")
PLATFORMS = ("kubernetes", "openshift")
TEST_DIGEST = "sha256:" + ("a" * 64)


def fail(message: str) -> None:
    raise SystemExit(message)


def render(chart: pathlib.Path, *extra: str) -> list[dict]:
    completed = subprocess.run(
        ["helm", "template", "image-contract", str(chart), *extra],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        fail(f"{chart}: helm template failed:\n{completed.stderr.strip()}")
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def driver_images(docs: list[dict], protocol: str) -> list[str]:
    name = f"{protocol}-csi-ngxstorage"
    images: list[str] = []
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "DaemonSet"}:
            continue
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            if container.get("name") == name:
                images.append(container.get("image", ""))
    return images


def main() -> int:
    for protocol in PROTOCOLS:
        for platform in PLATFORMS:
            chart = ROOT / protocol / platform
            values = yaml.safe_load((chart / "values.yaml").read_text())
            image = values.get("image", {})
            repository = image.get("repository")
            tag = image.get("tag")
            digest = image.get("digest", "")
            if not repository or not tag:
                fail(f"{protocol}/{platform}: image.repository and image.tag fallback are required")

            defaults = driver_images(render(chart), protocol)
            expected_default = f"{repository}@{digest}" if digest else f"{repository}:{tag}"
            if len(defaults) != 2 or set(defaults) != {expected_default}:
                fail(f"{protocol}/{platform}: default driver image render mismatch: {defaults}")

            pinned = driver_images(
                render(chart, "--set-string", f"image.digest={TEST_DIGEST}"), protocol
            )
            expected_pinned = f"{repository}@{TEST_DIGEST}"
            if len(pinned) != 2 or set(pinned) != {expected_pinned}:
                fail(f"{protocol}/{platform}: digest driver image render mismatch: {pinned}")

            invalid = subprocess.run(
                [
                    "helm",
                    "template",
                    "image-contract-invalid",
                    str(chart),
                    "--set-string",
                    "image.digest=sha256:bad",
                ],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if invalid.returncode == 0:
                fail(f"{protocol}/{platform}: malformed image.digest was accepted")
            print(f"{protocol}/{platform}: tag fallback + digest override contract passed")

    print("image reference contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
