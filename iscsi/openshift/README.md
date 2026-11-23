# NGX Storage iSCSI CSI Driver

Installs the NGX Storage iSCSI CSI controller, node DaemonSet, RBAC,
StorageClass, and optional VolumeSnapshotClass on Red Hat OpenShift.

Use `values.yaml` or `values.schema.json` to configure the image, backend
Secret, placement, and iSCSI target names. The release namespace is selected
with Helm's `--namespace` option; this chart also supplies OpenShift SCC
resources.
