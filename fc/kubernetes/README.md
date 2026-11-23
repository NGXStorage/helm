# NGX Storage FC CSI Driver

Installs the NGX Storage Fibre Channel CSI controller, node DaemonSet, RBAC,
StorageClass, and optional VolumeSnapshotClass on upstream Kubernetes.

Use `values.yaml` or `values.schema.json` to configure the image, backend
Secret, placement, and FC target names. The release namespace is selected with
Helm's `--namespace` option.
