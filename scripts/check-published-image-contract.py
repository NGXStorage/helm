#!/usr/bin/env python3
"""Verify that chart-default CSI images exist and match hardening contracts."""

import io
import json
import os
import pathlib
import re
import sys
import tarfile
import urllib.error
import urllib.request

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
INTEGRATIONS = ROOT.parent
ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
MAX_INSPECTED_LAYER = 1024 * 1024


class ImageContractError(ValueError):
    pass


def get(url, accept=None):
    headers = {"Accept": accept} if accept else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise ImageContractError(f"{url}: registry returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ImageContractError(f"{url}: registry request failed: {error.reason}") from error


def split_image(reference):
    if "@" in reference:
        repository, identifier = reference.rsplit("@", 1)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
            raise ImageContractError(f"invalid immutable image digest: {reference}")
    else:
        repository, identifier = reference.rsplit(":", 1)
        if identifier == "latest":
            raise ImageContractError(f":latest is forbidden for a release image: {reference}")
    if "/" not in repository:
        raise ImageContractError(f"fully-qualified registry path required: {reference}")
    registry, path = repository.split("/", 1)
    return registry, path, identifier


def chart_image(protocol, platform):
    values = yaml.safe_load((ROOT / protocol / platform / "values.yaml").read_text())
    image = values.get("image", {})
    repository = image.get("repository")
    tag = image.get("tag")
    digest = image.get("digest") or ""
    if not repository or not tag:
        raise ImageContractError(f"{protocol}/{platform}: image.repository and image.tag are required")
    if digest:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ImageContractError(f"{protocol}/{platform}: invalid image.digest {digest!r}")
        return f"{repository}@{digest}"
    return f"{repository}:{tag}"


def manual_manifest_base(protocol):
    folder = {"fc": "FC", "iscsi": "iSCSI", "nfs": "NFS"}[protocol]
    return INTEGRATIONS / "Kubernetes" / "CSI" / "Driver" / folder / "deploy" / "manifest" / "base"


def manual_images(protocol):
    base = manual_manifest_base(protocol)
    expected_container = f"{protocol}-csi-ngxstorage"
    images = []
    for suffix in ("controller", "node"):
        path = base / f"{protocol}-csi-ngxstorage-{suffix}.yaml"
        docs = [doc for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]
        kind = "Deployment" if suffix == "controller" else "DaemonSet"
        workloads = [doc for doc in docs if doc.get("kind") == kind and doc.get("spec", {}).get("selector")]
        if len(workloads) != 1:
            raise ImageContractError(f"{protocol}: expected one manual {kind} in {path}")
        containers = workloads[0].get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        matching = [container.get("image", "") for container in containers if container.get("name") == expected_container]
        if len(matching) != 1 or not matching[0]:
            raise ImageContractError(f"{protocol}: manual {kind} driver image is missing or ambiguous")
        images.append(matching[0])
    return images


def entrypoint_from_layers(registry, path, manifest):
    for layer in reversed(manifest.get("layers", [])):
        if layer.get("size", MAX_INSPECTED_LAYER + 1) > MAX_INSPECTED_LAYER:
            continue
        blob = get(f"https://{registry}/v2/{path}/blobs/{layer['digest']}")
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.name.lstrip("./") == "entrypoint.sh":
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            break
                        return extracted.read().decode("utf-8")
        except tarfile.TarError:
            continue
    raise ImageContractError("entrypoint.sh was not found in inspectable image layers")


def validate_image(protocol, reference):
    violations = []
    registry, path, identifier = split_image(reference)
    manifest_url = f"https://{registry}/v2/{path}/manifests/{identifier}"
    manifest = json.loads(get(manifest_url, ACCEPT))
    config_digest = manifest.get("config", {}).get("digest")
    if not config_digest:
        raise ImageContractError("Docker v2 image config digest is missing")
    config = json.loads(get(f"https://{registry}/v2/{path}/blobs/{config_digest}"))
    labels = config.get("config", {}).get("Labels", {}) or {}
    revision = labels.get("org.opencontainers.image.revision")
    vcs_ref = labels.get("vcs-ref")
    if not revision or revision == "unknown" or not vcs_ref or vcs_ref == "unknown":
        violations.append("OCI revision/vcs-ref provenance is missing or unknown")

    entrypoint = entrypoint_from_layers(registry, path, manifest)
    if re.search(r"/tmp(?:/|\s|$)", entrypoint):
        violations.append("entrypoint references forbidden runtime path /tmp")
    mutation = re.compile(
        r"(?:^|\s)(?:ln|rm|mv|cp|install|mkdir|touch|truncate|chmod|chown)\s+[^\n]*\s/etc(?:/|\s|$)",
        re.MULTILINE,
    )
    if mutation.search(entrypoint):
        violations.append("entrypoint mutates immutable /etc")
    expected_binary = f"/usr/local/bin/{protocol}-csi-ngxstorage-driver"
    if f'exec {expected_binary} "$@"' not in entrypoint:
        violations.append(f"entrypoint does not exec {expected_binary}")
    if violations:
        raise ImageContractError("; ".join(violations))


def main():
    failures = []
    require_digest = os.environ.get("REQUIRE_IMAGE_DIGEST", "0") == "1"
    for protocol in ("fc", "iscsi", "nfs"):
        try:
            kubernetes_image = chart_image(protocol, "kubernetes")
            openshift_image = chart_image(protocol, "openshift")
            if kubernetes_image != openshift_image:
                raise ImageContractError(
                    f"Kubernetes/OpenShift default image drift: {kubernetes_image} != {openshift_image}"
                )
            # Manual-manifest parity is a workspace check: the standalone
            # chart repository checkout does not contain the sibling driver
            # manifests. Chart defaults and the registry contract stay
            # mandatory in every checkout.
            if manual_manifest_base(protocol).is_dir():
                manual = manual_images(protocol)
                if manual != [kubernetes_image, kubernetes_image]:
                    raise ImageContractError(
                        f"Helm/manual default image drift: helm={kubernetes_image} manual={manual}"
                    )
            else:
                print(f"{protocol}: manual-manifest parity skipped (driver workspace not present)")
            image = os.environ.get(f"{protocol.upper()}_IMAGE", kubernetes_image)
            if require_digest and "@sha256:" not in image:
                raise ImageContractError(
                    f"release closure requires an immutable digest reference, got {image}"
                )
            validate_image(protocol, image)
            print(f"{protocol}: published image contract passed: {image}")
        except (ImageContractError, json.JSONDecodeError, KeyError, ValueError) as error:
            failures.append(f"{protocol}: {error}")
    if failures:
        print("published image contract failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
