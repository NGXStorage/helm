# NGX Storage NFS CSI Driver

Installs the NGX Storage NFS CSI controller, node DaemonSet, RBAC,
StorageClass, and optional VolumeSnapshotClass on upstream Kubernetes.

Use `values.yaml` or `values.schema.json` to configure the image, backend
Secret, placement, and NFS target names. The release namespace is selected with
Helm's `--namespace` option.
