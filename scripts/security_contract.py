#!/usr/bin/env python3
"""Semantic security contract shared by Helm positive and negative tests."""


class ContractError(ValueError):
    """A rendered chart violates the CSI container security contract."""


def _fail(message):
    raise ContractError(message)


def _one(docs, kind, protocol, platform):
    matches = [doc for doc in docs if doc.get("kind") == kind]
    if len(matches) != 1:
        _fail(f"{protocol}/{platform}: expected one {kind}, got {len(matches)}")
    return matches[0]


def _reject_tmp_mounts(containers, protocol, platform):
    for container in containers:
        for mount in container.get("volumeMounts", []):
            mount_path = mount.get("mountPath", "")
            if mount_path == "/tmp" or mount_path.startswith("/tmp/"):
                _fail(
                    f"{protocol}/{platform}: container {container['name']} "
                    f"mounts forbidden runtime path {mount_path}"
                )


def _env_by_name(container):
    return {item.get("name"): item for item in container.get("env", []) if item.get("name")}


def _require_downward_env(env, name, field_path, protocol, platform):
    item = env.get(name, {})
    actual = item.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
    if actual != field_path:
        _fail(f"{protocol}/{platform}: {name} must come from {field_path}, got {actual!r}")


def _role_by_name(docs, name, protocol, platform):
    matches = [doc for doc in docs if doc.get("kind") == "Role" and doc.get("metadata", {}).get("name") == name]
    if len(matches) != 1:
        _fail(f"{protocol}/{platform}: expected one Role {name}, got {len(matches)}")
    return matches[0]


def _binding_by_name(docs, name, protocol, platform):
    matches = [doc for doc in docs if doc.get("kind") == "RoleBinding" and doc.get("metadata", {}).get("name") == name]
    if len(matches) != 1:
        _fail(f"{protocol}/{platform}: expected one RoleBinding {name}, got {len(matches)}")
    return matches[0]


def _validate_binding(binding, role_name, service_account, protocol, platform):
    if binding.get("roleRef", {}).get("kind") != "Role" or binding.get("roleRef", {}).get("name") != role_name:
        _fail(f"{protocol}/{platform}: {binding['metadata']['name']} has wrong roleRef")
    subjects = binding.get("subjects", [])
    if len(subjects) != 1 or subjects[0].get("kind") != "ServiceAccount" or subjects[0].get("name") != service_account:
        _fail(
            f"{protocol}/{platform}: {binding['metadata']['name']} must bind only ServiceAccount {service_account}"
        )


def _validate_operation_role(role, expected_names, protocol, platform):
    rules = role.get("rules", [])
    if len(rules) != 1:
        _fail(f"{protocol}/{platform}: {role['metadata']['name']} must contain exactly one rule")
    rule = rules[0]
    if rule.get("apiGroups") != ["coordination.k8s.io"] or rule.get("resources") != ["leases"]:
        _fail(f"{protocol}/{platform}: {role['metadata']['name']} is not Lease-only")
    if set(rule.get("resourceNames", [])) != expected_names or len(rule.get("resourceNames", [])) != len(expected_names):
        _fail(f"{protocol}/{platform}: {role['metadata']['name']} resourceNames do not exactly match the Lease pool")
    if set(rule.get("verbs", [])) != {"get", "update", "patch"} or len(rule.get("verbs", [])) != 3:
        _fail(f"{protocol}/{platform}: {role['metadata']['name']} has overbroad Lease verbs {rule.get('verbs', [])}")


def _validate_coordination(docs, protocol, platform, controller_spec, node_spec):
    controller_main = [
        container for container in controller_spec["containers"] if container["name"].endswith("-csi-ngxstorage")
    ]
    if len(controller_main) != 1:
        _fail(f"{protocol}/{platform}: cannot identify exactly one main controller container")
    controller_main = controller_main[0]
    controller_env = _env_by_name(controller_main)
    prefix = controller_env.get("NGX_COORDINATION_LEASE_PREFIX", {}).get("value")
    count_raw = controller_env.get("NGX_COORDINATION_SHARD_COUNT", {}).get("value")
    try:
        count = int(count_raw)
    except (TypeError, ValueError):
        _fail(f"{protocol}/{platform}: invalid NGX_COORDINATION_SHARD_COUNT {count_raw!r}")
    if count != 128:
        _fail(f"{protocol}/{platform}: default coordination shard count = {count}, want 128")
    if not prefix or controller_spec.get("serviceAccountName") != f"{prefix}-controller":
        _fail(f"{protocol}/{platform}: controller Lease prefix/ServiceAccount identity mismatch")
    for name, field_path in (("POD_NAMESPACE", "metadata.namespace"), ("POD_NAME", "metadata.name"), ("POD_UID", "metadata.uid")):
        _require_downward_env(controller_env, name, field_path, protocol, platform)

    guard_name = f"{prefix}-coordination-config"
    guards = [
        doc for doc in docs
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == guard_name
    ]
    if len(guards) != 1:
        _fail(f"{protocol}/{platform}: immutable coordination ConfigMap {guard_name} missing or duplicated")
    guard = guards[0]
    if guard.get("immutable") is not True:
        _fail(f"{protocol}/{platform}: coordination ConfigMap must be immutable")
    if guard.get("data") != {"lease-prefix": prefix, "shard-count": str(count)}:
        _fail(f"{protocol}/{platform}: coordination ConfigMap data does not match runtime lock domain")

    expected_names = {f"{prefix}-operation-lock-{index:03d}" for index in range(count)}
    leases = [doc for doc in docs if doc.get("kind") == "Lease"]
    lease_names = [doc.get("metadata", {}).get("name") for doc in leases]
    if len(leases) != count or set(lease_names) != expected_names:
        _fail(
            f"{protocol}/{platform}: operation Lease pool mismatch: count={len(leases)} expected={count}"
        )

    controller_role_name = f"{prefix}-controller-operation-lock"
    controller_role = _role_by_name(docs, controller_role_name, protocol, platform)
    _validate_operation_role(controller_role, expected_names, protocol, platform)
    _validate_binding(
        _binding_by_name(docs, controller_role_name, protocol, platform),
        controller_role_name,
        f"{prefix}-controller",
        protocol,
        platform,
    )

    node_main = [container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage")]
    if len(node_main) != 1:
        _fail(f"{protocol}/{platform}: cannot identify exactly one main node container for coordination")
    node_main = node_main[0]
    node_env = _env_by_name(node_main)
    token_volume_name = "kube-api-access-ngxstorage"

    if protocol == "nfs":
        if any(name in node_env for name in ("NGX_COORDINATION_LEASE_PREFIX", "NGX_COORDINATION_SHARD_COUNT", "POD_NAME", "POD_UID")):
            _fail(f"{protocol}/{platform}: NFS node unexpectedly receives controller coordination environment")
        if any(volume.get("name") == token_volume_name for volume in node_spec.get("volumes", [])):
            _fail(f"{protocol}/{platform}: NFS node unexpectedly receives projected Kubernetes credentials")
        if any(
            mount.get("name") == token_volume_name
            for container in node_spec["containers"]
            for mount in container.get("volumeMounts", [])
        ):
            _fail(f"{protocol}/{platform}: NFS node unexpectedly mounts projected Kubernetes credentials")
        return

    if node_spec.get("automountServiceAccountToken") is not False:
        _fail(f"{protocol}/{platform}: block node pod must keep automountServiceAccountToken=false")
    if node_env.get("NGX_COORDINATION_LEASE_PREFIX", {}).get("value") != prefix:
        _fail(f"{protocol}/{platform}: controller/node Lease prefixes differ")
    if node_env.get("NGX_COORDINATION_SHARD_COUNT", {}).get("value") != str(count):
        _fail(f"{protocol}/{platform}: controller/node shard counts differ")
    for name, field_path in (("POD_NAMESPACE", "metadata.namespace"), ("POD_NAME", "metadata.name"), ("POD_UID", "metadata.uid")):
        _require_downward_env(node_env, name, field_path, protocol, platform)
    _require_downward_env(node_env, "KUBE_NODE_NAME", "spec.nodeName", protocol, platform)

    token_volumes = [volume for volume in node_spec.get("volumes", []) if volume.get("name") == token_volume_name]
    if len(token_volumes) != 1 or "projected" not in token_volumes[0]:
        _fail(f"{protocol}/{platform}: block node projected API credential volume missing")
    sources = token_volumes[0]["projected"].get("sources", [])
    token_sources = [source.get("serviceAccountToken") for source in sources if source.get("serviceAccountToken")]
    ca_sources = [source.get("configMap") for source in sources if source.get("configMap")]
    ns_sources = [source.get("downwardAPI") for source in sources if source.get("downwardAPI")]
    if len(token_sources) != 1 or token_sources[0].get("path") != "token" or token_sources[0].get("expirationSeconds") != 3600:
        _fail(f"{protocol}/{platform}: projected serviceAccountToken contract failed")
    if len(ca_sources) != 1 or ca_sources[0].get("name") != "kube-root-ca.crt":
        _fail(f"{protocol}/{platform}: projected kube-root-ca.crt source missing")
    if len(ns_sources) != 1 or not any(
        item.get("path") == "namespace" and item.get("fieldRef", {}).get("fieldPath") == "metadata.namespace"
        for item in ns_sources[0].get("items", [])
    ):
        _fail(f"{protocol}/{platform}: projected namespace source missing")

    token_mounts = [
        (container["name"], mount)
        for container in node_spec["containers"]
        for mount in container.get("volumeMounts", [])
        if mount.get("name") == token_volume_name
    ]
    if len(token_mounts) != 1 or token_mounts[0][0] != node_main["name"]:
        _fail(f"{protocol}/{platform}: projected API credential must be mounted only by the main node container")
    token_mount = token_mounts[0][1]
    if token_mount.get("mountPath") != "/var/run/secrets/kubernetes.io/serviceaccount" or token_mount.get("readOnly") is not True:
        _fail(f"{protocol}/{platform}: projected API credential mount path/readOnly contract failed")

    node_role_name = f"{prefix}-node-operation-lock"
    _validate_operation_role(_role_by_name(docs, node_role_name, protocol, platform), expected_names, protocol, platform)
    _validate_binding(
        _binding_by_name(docs, node_role_name, protocol, platform),
        node_role_name,
        f"{prefix}-node",
        protocol,
        platform,
    )
    pod_role_name = f"{prefix}-node-holder-pod-read"
    pod_role = _role_by_name(docs, pod_role_name, protocol, platform)
    if pod_role.get("rules") != [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]:
        _fail(f"{protocol}/{platform}: node holder Pod liveness Role is broader than pods/get")
    _validate_binding(
        _binding_by_name(docs, pod_role_name, protocol, platform),
        pod_role_name,
        f"{prefix}-node",
        protocol,
        platform,
    )


def validate_docs(docs, protocol, platform):
    """Validate one fully rendered Helm chart."""
    controller = _one(docs, "Deployment", protocol, platform)
    node = _one(docs, "DaemonSet", protocol, platform)

    controller_template = controller["spec"]["template"]
    controller_spec = controller_template["spec"]
    for forbidden in ("hostNetwork", "hostPID", "hostIPC"):
        if controller_spec.get(forbidden):
            _fail(f"{protocol}/{platform}: controller sets {forbidden}")
    if any(volume.get("hostPath") for volume in controller_spec.get("volumes", [])):
        _fail(f"{protocol}/{platform}: controller uses hostPath")
    _reject_tmp_mounts(controller_spec["containers"], protocol, platform)
    for container in controller_spec["containers"]:
        security = container.get("securityContext", {})
        if security.get("privileged") or security.get("allowPrivilegeEscalation") is not False:
            _fail(f"{protocol}/{platform}: controller container {container['name']} escalates")
        if security.get("readOnlyRootFilesystem") is not True:
            _fail(f"{protocol}/{platform}: controller container {container['name']} is not read-only")
        if "ALL" not in security.get("capabilities", {}).get("drop", []):
            _fail(f"{protocol}/{platform}: controller container {container['name']} does not drop ALL")

    node_template = node["spec"]["template"]
    node_spec = node_template["spec"]
    for required in ("hostNetwork", "hostPID", "hostIPC"):
        if node_spec.get(required) is not True:
            _fail(f"{protocol}/{platform}: node is missing {required}")
    _reject_tmp_mounts(node_spec["containers"], protocol, platform)

    main_matches = [container for container in node_spec["containers"] if container["name"].endswith("-csi-ngxstorage")]
    if len(main_matches) != 1:
        _fail(f"{protocol}/{platform}: cannot identify exactly one main node container")
    main = main_matches[0]
    main_security = main.get("securityContext", {})
    if main_security.get("privileged") is not True:
        _fail(f"{protocol}/{platform}: main node container is not privileged")
    if main_security.get("readOnlyRootFilesystem") is not True:
        _fail(f"{protocol}/{platform}: main node container rootfs is not read-only")
    for capability in ("SYS_ADMIN", "SYS_CHROOT"):
        if capability not in main_security.get("capabilities", {}).get("add", []):
            _fail(f"{protocol}/{platform}: main node container lacks {capability}")

    required_mounts = {"/csi", "/var/lib/kubelet", "/host", "/dev"}
    mount_paths = {mount.get("mountPath") for mount in main.get("volumeMounts", [])}
    if not required_mounts.issubset(mount_paths):
        _fail(f"{protocol}/{platform}: main node mounts are incomplete")
    for mount in main.get("volumeMounts", []):
        if mount.get("mountPath") in {"/var/lib/kubelet", "/host"} and mount.get("mountPropagation") != "Bidirectional":
            _fail(f"{protocol}/{platform}: {mount['mountPath']} is not Bidirectional")

    expected_log = f"/host/var/log/ngxstorage/{protocol}"
    log_values = {
        env.get("value")
        for env in main.get("env", [])
        if env.get("name") == "NGXSTORAGE_CSI_LOG_DIR"
    }
    if expected_log not in log_values:
        _fail(f"{protocol}/{platform}: node log path contract failed")

    expected_port = {"fc": 9811, "iscsi": 9812, "nfs": 9809}[protocol]
    if main.get("livenessProbe", {}).get("httpGet", {}).get("port") != expected_port:
        _fail(f"{protocol}/{platform}: node liveness port contract failed")
    liveness_ports = [
        container.get("livenessProbe", {}).get("httpGet", {}).get("port")
        for container in node_spec["containers"]
    ]
    liveness_ports = [port for port in liveness_ports if port is not None]
    if len(liveness_ports) != len(set(liveness_ports)):
        _fail(f"{protocol}/{platform}: duplicate node liveness port")

    if platform == "kubernetes" and node_template.get("metadata", {}).get("annotations", {}).get("openshift.io/required-scc"):
        _fail(f"{protocol}/{platform}: Kubernetes node requests SCC")
    if controller_template.get("metadata", {}).get("annotations", {}).get("openshift.io/required-scc"):
        _fail(f"{protocol}/{platform}: controller requests node SCC")

    for container in node_spec["containers"]:
        if container is main:
            continue
        security = container.get("securityContext", {})
        if security.get("privileged") or security.get("allowPrivilegeEscalation") is not False:
            _fail(f"{protocol}/{platform}: node sidecar {container['name']} escalates")
        if security.get("readOnlyRootFilesystem") is not True:
            _fail(f"{protocol}/{platform}: node sidecar {container['name']} is not read-only")
        if "ALL" not in security.get("capabilities", {}).get("drop", []):
            _fail(f"{protocol}/{platform}: node sidecar {container['name']} does not drop ALL")

    _validate_coordination(docs, protocol, platform, controller_spec, node_spec)

    sccs = [doc for doc in docs if doc.get("kind") == "SecurityContextConstraints"]
    roles = [doc for doc in docs if doc.get("kind") == "Role"]
    bindings = [doc for doc in docs if doc.get("kind") == "RoleBinding"]
    if platform == "kubernetes":
        if sccs:
            _fail(f"{protocol}/{platform}: Kubernetes chart contains SCC")
        return

    if len(sccs) != 1 or not roles or not bindings:
        _fail(f"{protocol}/{platform}: SCC/use Role/RoleBinding set is incomplete")
    scc = sccs[0]
    if scc.get("users") or scc.get("groups"):
        _fail(f"{protocol}/{platform}: SCC directly grants users/groups")
    scc_name = scc["metadata"]["name"]
    use_roles = [
        role
        for role in roles
        if any(
            rule.get("resources") == ["securitycontextconstraints"]
            and "use" in rule.get("verbs", [])
            and rule.get("resourceNames") == [scc_name]
            for rule in role.get("rules", [])
        )
    ]
    if len(use_roles) != 1:
        _fail(f"{protocol}/{platform}: exact SCC use Role missing or duplicated")
    role_name = use_roles[0]["metadata"]["name"]
    scoped_bindings = [binding for binding in bindings if binding.get("roleRef", {}).get("name") == role_name]
    expected_node_sa = node_spec.get("serviceAccountName")
    valid_bindings = [
        binding
        for binding in scoped_bindings
        if len(binding.get("subjects", [])) == 1
        and binding["subjects"][0].get("kind") == "ServiceAccount"
        and binding["subjects"][0].get("name") == expected_node_sa
    ]
    if len(valid_bindings) != 1:
        _fail(f"{protocol}/{platform}: SCC RoleBinding does not bind only the node ServiceAccount")
    expected_scc = node_template.get("metadata", {}).get("annotations", {}).get("openshift.io/required-scc")
    if expected_scc != scc_name:
        _fail(f"{protocol}/{platform}: node required-scc annotation mismatch")
