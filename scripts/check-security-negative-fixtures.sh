#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export ROOT_DIR

python3 - <<'PY'
import copy
import os
import pathlib
import subprocess
import sys

import yaml

root = pathlib.Path(os.environ["ROOT_DIR"])
sys.path.insert(0, str(root / "scripts"))
from security_contract import ContractError, validate_docs


def render(protocol, platform):
    rendered = subprocess.check_output(
        ["helm", "template", "security-negative", str(root / protocol / platform), "--namespace", "ngxstorage"],
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def one(docs, kind):
    matches = [doc for doc in docs if doc.get("kind") == kind]
    if len(matches) != 1:
        raise AssertionError(f"expected one {kind}, got {len(matches)}")
    return matches[0]


def expect_invalid(label, docs, protocol, platform):
    try:
        validate_docs(docs, protocol, platform)
    except ContractError as error:
        print(f"negative fixture detected: {label}: {error}")
        return
    raise AssertionError(f"negative fixture was accepted by validator: {label}")


for protocol in ("fc", "iscsi", "nfs"):
    for platform in ("kubernetes", "openshift"):
        baseline = render(protocol, platform)
        validate_docs(baseline, protocol, platform)

        if platform == "openshift":
            mutated = copy.deepcopy(baseline)
            one(mutated, "SecurityContextConstraints")["users"] = ["system:serviceaccount:wrong:wrong-node"]
            expect_invalid(f"{protocol}/{platform}: SCC direct users grant", mutated, protocol, platform)

            mutated = copy.deepcopy(baseline)
            scc = one(mutated, "SecurityContextConstraints")
            use_role = next(
                role for role in mutated
                if role.get("kind") == "Role"
                and any(rule.get("resourceNames") == [scc["metadata"]["name"]] for rule in role.get("rules", []))
            )
            binding = next(
                item for item in mutated
                if item.get("kind") == "RoleBinding"
                and item.get("roleRef", {}).get("name") == use_role["metadata"]["name"]
            )
            binding["subjects"][0]["name"] = "wrong-controller"
            expect_invalid(f"{protocol}/{platform}: controller SCC RoleBinding subject", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        controller = one(mutated, "Deployment")
        controller["spec"]["template"]["spec"]["containers"][0]["securityContext"]["privileged"] = True
        expect_invalid(f"{protocol}/{platform}: privileged controller", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
        main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
        sidecar = next(container for container in node_spec["containers"] if container is not main)
        sidecar["securityContext"]["capabilities"]["drop"] = []
        expect_invalid(f"{protocol}/{platform}: sidecar capability drop", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
        main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
        next(mount for mount in main["volumeMounts"] if mount.get("mountPath") == "/host")["mountPropagation"] = "HostToContainer"
        expect_invalid(f"{protocol}/{platform}: host mount propagation", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
        main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
        next(env for env in main["env"] if env.get("name") == "NGXSTORAGE_CSI_LOG_DIR")["value"] = "/tmp/ngxstorage"
        expect_invalid(f"{protocol}/{platform}: protocol log path", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
        main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
        sidecar = next(container for container in node_spec["containers"] if container is not main)
        sidecar["livenessProbe"] = copy.deepcopy(main["livenessProbe"])
        expect_invalid(f"{protocol}/{platform}: duplicate liveness port", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
        main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
        node_spec.setdefault("volumes", []).append({"name": "tmp-dir", "emptyDir": {}})
        main.setdefault("volumeMounts", []).append({"name": "tmp-dir", "mountPath": "/tmp"})
        expect_invalid(f"{protocol}/{platform}: forbidden /tmp mount", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        controller = one(mutated, "Deployment")["spec"]["template"]["spec"]
        controller_main = next(container for container in controller["containers"] if container["name"].endswith("-csi-ngxstorage"))
        prefix = next(env["value"] for env in controller_main["env"] if env.get("name") == "NGX_COORDINATION_LEASE_PREFIX")
        operation_role = next(
            role for role in mutated
            if role.get("kind") == "Role"
            and role.get("metadata", {}).get("name") == f"{prefix}-controller-operation-lock"
        )
        operation_role["rules"][0]["verbs"].append("create")
        expect_invalid(f"{protocol}/{platform}: overbroad operation Lease create", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        first_lease = next(index for index, doc in enumerate(mutated) if doc.get("kind") == "Lease")
        del mutated[first_lease]
        expect_invalid(f"{protocol}/{platform}: missing operation Lease shard", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        guard = next(
            doc for doc in mutated
            if doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name") == f"{prefix}-coordination-config"
        )
        guard["immutable"] = False
        expect_invalid(f"{protocol}/{platform}: mutable shard-count migration guard", mutated, protocol, platform)

        mutated = copy.deepcopy(baseline)
        guard = next(
            doc for doc in mutated
            if doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name") == f"{prefix}-coordination-config"
        )
        guard["data"]["shard-count"] = "256"
        expect_invalid(f"{protocol}/{platform}: configured/guard shard-count mismatch", mutated, protocol, platform)

        if protocol in {"fc", "iscsi"}:
            mutated = copy.deepcopy(baseline)
            node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
            main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
            sidecar = next(container for container in node_spec["containers"] if container is not main)
            token_mount = copy.deepcopy(next(mount for mount in main["volumeMounts"] if mount.get("name") == "kube-api-access-ngxstorage"))
            sidecar.setdefault("volumeMounts", []).append(token_mount)
            expect_invalid(f"{protocol}/{platform}: projected token leaked to node sidecar", mutated, protocol, platform)
        else:
            mutated = copy.deepcopy(baseline)
            node_spec = one(mutated, "DaemonSet")["spec"]["template"]["spec"]
            main = next(container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage"))
            main.setdefault("env", []).append({"name": "NGX_COORDINATION_SHARD_COUNT", "value": "128"})
            expect_invalid(f"{protocol}/{platform}: NFS node coordination credential surface", mutated, protocol, platform)

print("negative security fixtures passed")
PY
