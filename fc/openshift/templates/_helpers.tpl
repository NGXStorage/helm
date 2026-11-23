{{- /*
  ngxstorage.driverName computes the effective CSI driver name.
  An empty driverNamePrefix preserves the canonical fc.csi.ngxstorage.com;
  a non-empty prefix is prepended as <prefix>.fc.csi.ngxstorage.com so a
  second driver instance can register a distinct CSIDriver on one cluster.
  The prefix is coerced to a string because Helm --set can pass a numeric
  label (for example --set driverNamePrefix=201).
*/ -}}
{{- define "ngxstorage.driverName" -}}
{{- $prefix := toString (.Values.driverNamePrefix | default "") | trimSuffix "." | lower -}}
{{- if $prefix -}}{{- $prefix -}}.{{- end -}}fc.csi.ngxstorage.com
{{- end -}}

{{- /*
  ngxstorage.fullname returns the base resource name for one driver instance.
  An empty driverNamePrefix keeps the canonical fc-csi-ngxstorage names; a
  distinct prefix produces <prefix>-fc-csi-ngxstorage so a second driver
  instance owns non-colliding cluster-scoped resources (StorageClass,
  VolumeSnapshotClass, ClusterRole, ClusterRoleBinding) while the namespaced
  DaemonSet/Deployment live in the configured namespace.
*/ -}}
{{- define "ngxstorage.fullname" -}}
{{- $prefix := toString (.Values.driverNamePrefix | default "") | trimSuffix "-" | lower -}}
{{- if $prefix -}}{{- printf "%s-fc-csi-ngxstorage" $prefix -}}{{- else -}}fc-csi-ngxstorage{{- end -}}
{{- end -}}

{{- /* Prefer an immutable digest when configured; tag is the development fallback. */ -}}
{{- define "ngxstorage.image" -}}
{{- $digest := toString (.Values.image.digest | default "") | trim -}}
{{- if $digest -}}{{- printf "%s@%s" .Values.image.repository $digest -}}{{- else -}}{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}{{- end -}}
{{- end -}}
