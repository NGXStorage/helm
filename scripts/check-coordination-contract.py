#!/usr/bin/env python3
"""Validate Plan 11.1 distributed-coordination deployment contracts.

This intentionally complements the general security validator:
- exercises every supported shard modulus instead of only the production default,
- proves unsupported moduli are rejected by chart schemas,
- compares the default Helm coordination surface with the manual manifests.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
INTEGRATIONS = ROOT.parent
DRIVER_WORKSPACE = INTEGRATIONS / "Kubernetes" / "CSI" / "Driver"
SUPPORTED_COUNTS = (32, 64, 128, 256)
DEFAULT_COUNT = 128
PROTOCOLS = {
    "fc": "FC",
    "iscsi": "iSCSI",
    "nfs": "NFS",
}
PLATFORMS = ("kubernetes", "openshift")


def fail(message: str) -> None:
    raise SystemExit(message)


def load_docs(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def render(protocol: str, platform: str, count: int = DEFAULT_COUNT, driver_prefix: str = "") -> list[dict]:
    command = [
        "helm",
        "template",
        f"coordination-{protocol}-{platform}",
        str(ROOT / protocol / platform),
        "--namespace",
        "ngxstorage-coordination",
        "--set",
        f"coordination.shardCount={count}",
    ]
    if driver_prefix:
        command.extend(["--set-string", f"driverNamePrefix={driver_prefix}"])
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        fail(
            f"{protocol}/{platform}: helm template failed for shardCount={count}:\n"
            f"{completed.stderr.strip()}"
        )
    return load_docs(completed.stdout)


def one(docs: list[dict], kind: str, name: str | None = None) -> dict:
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind
        and (name is None or doc.get("metadata", {}).get("name") == name)
    ]
    if len(matches) != 1:
        fail(f"expected exactly one {kind} {name or ''}, got {len(matches)}")
    return matches[0]


def main_container(workload: dict, prefix: str) -> dict:
    containers = workload["spec"]["template"]["spec"]["containers"]
    return one_container(containers, prefix)


def one_container(containers: list[dict], name: str) -> dict:
    matches = [container for container in containers if container.get("name") == name]
    if len(matches) != 1:
        fail(f"expected exactly one container {name}, got {len(matches)}")
    return matches[0]


def env_map(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", []) if item.get("name")}


def expected_lease_names(prefix: str, count: int) -> set[str]:
    return {f"{prefix}-operation-lock-{index:03d}" for index in range(count)}


def validate_operation_role(docs: list[dict], role_name: str, expected_names: set[str]) -> None:
    role = one(docs, "Role", role_name)
    rules = role.get("rules", [])
    if len(rules) != 1:
        fail(f"{role_name}: expected exactly one rule, got {len(rules)}")
    rule = rules[0]
    if rule.get("apiGroups") != ["coordination.k8s.io"] or rule.get("resources") != ["leases"]:
        fail(f"{role_name}: role must be coordination.k8s.io/leases only")
    names = rule.get("resourceNames", [])
    if len(names) != len(expected_names) or set(names) != expected_names:
        fail(f"{role_name}: resourceNames do not exactly match Lease pool")
    verbs = rule.get("verbs", [])
    if len(verbs) != 3 or set(verbs) != {"get", "update", "patch"}:
        fail(f"{role_name}: overbroad Lease verbs: {verbs}")


def validate_binding(docs: list[dict], binding_name: str, expected_sa: str) -> None:
    binding = one(docs, "RoleBinding", binding_name)
    if binding.get("roleRef", {}).get("kind") != "Role" or binding.get("roleRef", {}).get("name") != binding_name:
        fail(f"{binding_name}: wrong roleRef")
    subjects = binding.get("subjects", [])
    if (
        len(subjects) != 1
        or subjects[0].get("kind") != "ServiceAccount"
        or subjects[0].get("name") != expected_sa
    ):
        fail(f"{binding_name}: binding must target only ServiceAccount {expected_sa}")


def projected_token_volume(node_spec: dict) -> dict | None:
    matches = [
        volume
        for volume in node_spec.get("volumes", [])
        if volume.get("name") == "kube-api-access-ngxstorage"
    ]
    if not matches:
        return None
    if len(matches) != 1:
        fail(f"projected token volume duplicated: {len(matches)}")
    return matches[0]


def validate_render(protocol: str, platform: str, count: int, docs: list[dict], driver_prefix: str = "") -> set[str]:
    base = f"{protocol}-csi-ngxstorage"
    prefix = f"{driver_prefix}-{base}" if driver_prefix else base
    expected_names = expected_lease_names(prefix, count)
    leases = [doc for doc in docs if doc.get("kind") == "Lease"]
    names = [doc.get("metadata", {}).get("name") for doc in leases]
    if len(leases) != count or set(names) != expected_names:
        fail(
            f"{protocol}/{platform}: Lease pool count/name mismatch for shardCount={count}: "
            f"got={len(leases)} want={count}"
        )

    controller = one(docs, "Deployment", f"{prefix}-controller")
    controller_main = main_container(controller, base)
    controller_env = env_map(controller_main)
    if controller_env.get("NGX_COORDINATION_LEASE_PREFIX", {}).get("value") != prefix:
        fail(f"{protocol}/{platform}: controller prefix env mismatch")
    if controller_env.get("NGX_COORDINATION_SHARD_COUNT", {}).get("value") != str(count):
        fail(f"{protocol}/{platform}: controller shard-count env mismatch")

    guard = one(docs, "ConfigMap", f"{prefix}-coordination-config")
    if guard.get("immutable") is not True:
        fail(f"{protocol}/{platform}: coordination ConfigMap is not immutable")
    if guard.get("data") != {"lease-prefix": prefix, "shard-count": str(count)}:
        fail(f"{protocol}/{platform}: coordination ConfigMap does not match configured lock domain")

    controller_role = f"{prefix}-controller-operation-lock"
    validate_operation_role(docs, controller_role, expected_names)
    validate_binding(docs, controller_role, f"{prefix}-controller")

    node = one(docs, "DaemonSet", f"{prefix}-node")
    node_spec = node["spec"]["template"]["spec"]
    node_main = main_container(node, base)
    node_env = env_map(node_main)

    if protocol == "nfs":
        forbidden = {
            "NGX_COORDINATION_LEASE_PREFIX",
            "NGX_COORDINATION_SHARD_COUNT",
            "POD_NAMESPACE",
            "POD_NAME",
            "POD_UID",
        }
        leaked = forbidden.intersection(node_env)
        if leaked:
            fail(f"{protocol}/{platform}: NFS node coordination env leak: {sorted(leaked)}")
        if projected_token_volume(node_spec) is not None:
            fail(f"{protocol}/{platform}: NFS node unexpectedly has projected Kubernetes credentials")
    else:
        if node_spec.get("automountServiceAccountToken") is not False:
            fail(f"{protocol}/{platform}: node automountServiceAccountToken must remain false")
        if node_env.get("NGX_COORDINATION_LEASE_PREFIX", {}).get("value") != prefix:
            fail(f"{protocol}/{platform}: node/controller prefix mismatch")
        if node_env.get("NGX_COORDINATION_SHARD_COUNT", {}).get("value") != str(count):
            fail(f"{protocol}/{platform}: node/controller shard-count mismatch")

        node_role = f"{prefix}-node-operation-lock"
        validate_operation_role(docs, node_role, expected_names)
        validate_binding(docs, node_role, f"{prefix}-node")
        holder_role = one(docs, "Role", f"{prefix}-node-holder-pod-read")
        if holder_role.get("rules") != [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]:
            fail(f"{protocol}/{platform}: holder Pod Role must be exactly pods/get")
        validate_binding(docs, f"{prefix}-node-holder-pod-read", f"{prefix}-node")

        token_volume = projected_token_volume(node_spec)
        if token_volume is None or "projected" not in token_volume:
            fail(f"{protocol}/{platform}: projected node credential volume missing")
        token_mounts = [
            (container.get("name"), mount)
            for container in node_spec["containers"]
            for mount in container.get("volumeMounts", [])
            if mount.get("name") == "kube-api-access-ngxstorage"
        ]
        if len(token_mounts) != 1 or token_mounts[0][0] != base:
            fail(f"{protocol}/{platform}: projected credential is not main-container-only")

    return expected_names


def normalized_env(container: dict, names: set[str]) -> dict[str, dict]:
    env = env_map(container)
    return {name: env.get(name) for name in sorted(names)}


def normalized_role_rule(docs: list[dict], role_name: str) -> dict:
    return one(docs, "Role", role_name).get("rules", [None])[0]


def normalized_binding(docs: list[dict], binding_name: str) -> tuple[str | None, tuple[tuple[str | None, str | None], ...]]:
    binding = one(docs, "RoleBinding", binding_name)
    return (
        binding.get("roleRef", {}).get("name"),
        tuple(
            sorted((subject.get("kind"), subject.get("name")) for subject in binding.get("subjects", []))
        ),
    )


def load_manual(protocol: str) -> list[dict]:
    folder = PROTOCOLS[protocol]
    base = DRIVER_WORKSPACE / folder / "deploy" / "manifest" / "base"
    docs: list[dict] = []
    for filename in (
        f"{protocol}-csi-ngxstorage-coordination.yaml",
        f"{protocol}-csi-ngxstorage-controller.yaml",
        f"{protocol}-csi-ngxstorage-node.yaml",
    ):
        docs.extend(load_docs((base / filename).read_text()))
    return docs


def validate_manual_parity(protocol: str, helm_docs: list[dict]) -> None:
    manual_docs = load_manual(protocol)
    prefix = f"{protocol}-csi-ngxstorage"
    expected_names = expected_lease_names(prefix, DEFAULT_COUNT)

    manual_names = {doc.get("metadata", {}).get("name") for doc in manual_docs if doc.get("kind") == "Lease"}
    helm_names = {doc.get("metadata", {}).get("name") for doc in helm_docs if doc.get("kind") == "Lease"}
    if manual_names != expected_names or helm_names != expected_names:
        fail(f"{protocol}: Helm/manual default Lease pool parity failed")

    manual_guard = one(manual_docs, "ConfigMap", f"{prefix}-coordination-config")
    helm_guard = one(helm_docs, "ConfigMap", f"{prefix}-coordination-config")
    for guard in (manual_guard, helm_guard):
        guard.get("metadata", {}).pop("namespace", None)
        guard.get("metadata", {}).get("labels", {}).pop("app.kubernetes.io/instance", None)
    if manual_guard != helm_guard:
        fail(f"{protocol}: Helm/manual immutable coordination ConfigMap parity failed")

    role_names = [f"{prefix}-controller-operation-lock"]
    if protocol != "nfs":
        role_names.extend([f"{prefix}-node-operation-lock", f"{prefix}-node-holder-pod-read"])
    for role_name in role_names:
        if normalized_role_rule(manual_docs, role_name) != normalized_role_rule(helm_docs, role_name):
            fail(f"{protocol}: Helm/manual Role parity failed for {role_name}")
        if normalized_binding(manual_docs, role_name) != normalized_binding(helm_docs, role_name):
            fail(f"{protocol}: Helm/manual RoleBinding parity failed for {role_name}")

    controller_env_names = {
        "NGX_COORDINATION_LEASE_PREFIX",
        "NGX_COORDINATION_SHARD_COUNT",
        "POD_NAMESPACE",
        "POD_NAME",
        "POD_UID",
    }
    manual_controller = next(
        doc
        for doc in manual_docs
        if doc.get("kind") == "Deployment" and doc.get("spec", {}).get("selector")
    )
    helm_controller = one(helm_docs, "Deployment", f"{prefix}-controller")
    if normalized_env(main_container(manual_controller, prefix), controller_env_names) != normalized_env(
        main_container(helm_controller, prefix), controller_env_names
    ):
        fail(f"{protocol}: Helm/manual controller coordination env parity failed")

    manual_node = one(manual_docs, "DaemonSet", f"{prefix}-node")
    helm_node = one(helm_docs, "DaemonSet", f"{prefix}-node")
    if protocol == "nfs":
        if projected_token_volume(manual_node["spec"]["template"]["spec"]) is not None:
            fail("nfs: manual node unexpectedly contains projected Kubernetes credentials")
        return

    node_env_names = controller_env_names | {"KUBE_NODE_NAME"}
    if normalized_env(main_container(manual_node, prefix), node_env_names) != normalized_env(
        main_container(helm_node, prefix), node_env_names
    ):
        fail(f"{protocol}: Helm/manual node coordination env parity failed")

    manual_token = projected_token_volume(manual_node["spec"]["template"]["spec"])
    helm_token = projected_token_volume(helm_node["spec"]["template"]["spec"])
    if manual_token != helm_token:
        fail(f"{protocol}: Helm/manual projected token volume parity failed")

    def token_mount(workload: dict) -> dict:
        main = main_container(workload, prefix)
        mounts = [m for m in main.get("volumeMounts", []) if m.get("name") == "kube-api-access-ngxstorage"]
        if len(mounts) != 1:
            fail(f"{protocol}: expected one main token mount, got {len(mounts)}")
        return mounts[0]

    if token_mount(manual_node) != token_mount(helm_node):
        fail(f"{protocol}: Helm/manual projected token mount parity failed")


def validate_invalid_count_rejected(protocol: str, platform: str) -> None:
    command = [
        "helm",
        "template",
        "coordination-invalid",
        str(ROOT / protocol / platform),
        "--namespace",
        "ngxstorage-coordination",
        "--set",
        "coordination.shardCount=33",
    ]
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode == 0:
        fail(f"{protocol}/{platform}: unsupported shardCount=33 was accepted")


def main() -> int:
    for protocol in PROTOCOLS:
        default_helm_docs: list[dict] | None = None
        for platform in PLATFORMS:
            for count in SUPPORTED_COUNTS:
                docs = render(protocol, platform, count)
                validate_render(protocol, platform, count, docs)
                if platform == "kubernetes" and count == DEFAULT_COUNT:
                    default_helm_docs = docs
            validate_invalid_count_rejected(protocol, platform)
            print(f"{protocol}/{platform}: shard counts {SUPPORTED_COUNTS} and invalid-count rejection passed")

        if default_helm_docs is None:
            fail(f"{protocol}: default Kubernetes Helm render was not captured")
        # Manual-manifest parity is a workspace check: the standalone chart
        # repository checkout does not contain the sibling driver manifests.
        manual_base = DRIVER_WORKSPACE / PROTOCOLS[protocol] / "deploy" / "manifest" / "base"
        if manual_base.is_dir():
            validate_manual_parity(protocol, default_helm_docs)
            print(f"{protocol}: Helm/manual default coordination parity passed")
        else:
            print(f"{protocol}: manual-manifest parity skipped (driver workspace not present)")

        # The same protocol may be installed more than once. Prove the Lease
        # object names generated for two driver prefixes are disjoint.
        prefixed_sets = []
        for driver_prefix in ("201", "205"):
            docs = render(protocol, "kubernetes", 32, driver_prefix)
            prefixed_sets.append(validate_render(protocol, "kubernetes", 32, docs, driver_prefix))
        if prefixed_sets[0].intersection(prefixed_sets[1]):
            fail(f"{protocol}: prefixed coordination Lease pools collide")
        print(f"{protocol}: prefixed coordination Lease pools are disjoint")

    print("distributed coordination deployment contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
