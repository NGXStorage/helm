# NGX Storage CSI Helm charts

This repository publishes the NGX Storage CSI drivers for Fibre Channel (FC),
iSCSI, and NFS. Each driver has two charts:

| Protocol | Upstream Kubernetes | Red Hat OpenShift |
| --- | --- | --- |
| FC | `fc-csi-ngxstorage-kubernetes` | `fc-csi-ngxstorage-openshift` |
| iSCSI | `iscsi-csi-ngxstorage-kubernetes` | `iscsi-csi-ngxstorage-openshift` |
| NFS | `nfs-csi-ngxstorage-kubernetes` | `nfs-csi-ngxstorage-openshift` |

The `kubernetes` charts contain upstream resources. The `openshift` charts add
the OpenShift-specific SCC and pull-secret configuration. Neither variant
creates a namespace: choose it with Helm's `--namespace` option. This makes
separate releases and co-installation predictable.

## Use the published repository

```bash
helm repo add ngxstorage https://ngxstorage.github.io/helm
helm repo update ngxstorage
helm search repo ngxstorage
```

OpenShift users can register the same repository for Developer Console Form
View support:

```bash
oc apply -f openshift-helm-chart-repository.yaml
```

The `openshift-helm-chart-repository.yaml` file is in this checkout's root.

Every chart includes `values.schema.json`. OpenShift charts additionally carry
the `charts.openshift.io/name` metadata used by the Developer Console Form
View; YAML View remains available for advanced values.

## Install

The chart table above lists all six charts by protocol and platform. Pick the
one that matches your cluster. For a new installation, create the namespace and the backend Secret first.
The charts default to using an existing Secret. Read its values from files in
a private directory to keep the API key out of Helm release metadata.

The following is the single installation example; it uses FC on Kubernetes.
For NFS use the `nfs-csi-ngxstorage-<platform>` chart and add a `data-ip` file;
for iSCSI use the `iscsi-csi-ngxstorage-<platform>` chart and add a
`portal-group` file. For FC, `fc-target-names` is a comma-separated **array of
pre-created target names**, for example `target-a,target-b` — it is not a list
of WWPNs. Each file in `secret_dir` holds one value with no trailing newline;
`storage-ips` holds comma-separated management addresses.

```bash
release=fc-csi-ngxstorage
namespace=ngxstorage-fc
secret_dir=/path/to/private/secret-files   # storage-ips, pool-name, api-key, fc-target-names

kubectl create namespace "$namespace"
kubectl -n "$namespace" create secret generic "${release}-config" \
  --from-file=storage-ips="$secret_dir/storage-ips" \
  --from-file=pool-name="$secret_dir/pool-name" \
  --from-file=api-key="$secret_dir/api-key" \
  --from-file=fc-target-names="$secret_dir/fc-target-names"

helm upgrade --install "$release" ngxstorage/fc-csi-ngxstorage-kubernetes \
  --namespace "$namespace" --version 26.1.1 \
  --set secret.name="${release}-config" --wait --timeout 5m

kubectl -n "$namespace" get pods -o wide
kubectl get csidriver fc.csi.ngxstorage.com
```

## Co-installation

FC, iSCSI, and NFS can run in the same cluster. Install them in separate
namespaces as shown above, and use the platform-matching chart for every
release. Each chart has a protocol-specific CSI driver name and resource
names. If two instances of the same protocol serve different backends, set a
unique `driverNamePrefix` and use distinct Secret names. Do not install the
Kubernetes and OpenShift variant of the same protocol as simultaneous
releases; they are platform alternatives, not separate drivers.

The node DaemonSets use host networking for storage discovery. The charts use
distinct default liveness ports for FC, iSCSI, and NFS so all three can be
installed together; keep those ports distinct for additional same-protocol
instances on a host.

Node JSON logs are persisted on every host under
`/var/log/ngxstorage/{fc,iscsi,nfs}/*.log` and are visible inside the driver
container under `/host/var/log/ngxstorage/<protocol>`. Controller logs remain
on stdout and should be collected through the cluster logging stack.

## Distributed coordination and shard-count upgrades

Each chart pre-creates a reusable `coordination.k8s.io/v1` operation-lock Lease
pool. `coordination.shardCount` supports only `32`, `64`, `128`, and `256`; the
production default is `128`. The count controls hash-collision contention, not
capacity. More than 128 semantic operations can use a 128-shard pool over time
or concurrently; unrelated keys that hash to one shard simply serialize.

Operation-lock RBAC is namespaced and restricted to the exact rendered Lease
names with only `get`, `update`, and `patch`. FC and iSCSI node pods keep
`automountServiceAccountToken: false`; only the main CSI node container receives
the explicit projected service-account credential used by coordination.
Registrar/liveness sidecars do not receive that mount. Their node ServiceAccount
gets only `pods/get` for stale-holder Pod UID validation. NFS node pods do not
receive coordination credentials because NFS distributed locking is
controller-only.

The chart also creates an immutable `<fullname>-coordination-config` ConfigMap
containing the Lease prefix and shard count. This is an upgrade guard: a normal
rolling `helm upgrade` cannot change `128` to `256` (or any other modulus) while
the old coordination domain exists. Changing the modulus requires a coordinated
restart boundary: stop **all** controller/node workloads for that driver release,
verify no old pod remains, delete only the immutable coordination ConfigMap,
then run the Helm upgrade so the complete new Lease pool, exact RBAC and runtime
configuration are installed before the new generation starts. Never mix two
shard counts for one release.

For a coordination-safe uninstall, stop and wait for the driver Deployment and
DaemonSet first, then run the release removal so operation RBAC and Lease shards
cannot disappear under still-running CSI code. The Lease objects contain lock
metadata only; they do not store backend volume/share data or API credentials.
For diagnosis, inspect `<fullname>-operation-lock-*` with `kubectl get lease` or
`oc get lease`. Lease expiry is not storage fencing; the drivers re-read
authoritative state and fail closed where takeover safety is uncertain.

## Development

Run the checks from this repository root:

```bash
make test
make index REPO_URL=https://ngxstorage.github.io/helm
```

`make test` lints and renders all six charts, validates rendered YAML locally
(and uses strict `kubeconform` schema validation in CI), and checks that the
three protocol charts have no cluster-scoped resource or host-port collisions.
It also validates all supported coordination shard counts, rejects unsupported
moduli, checks Helm/manual coordination parity, and verifies least-privilege
Lease/token contracts with negative fixtures. Driver images support an optional
`image.digest`; when set it takes precedence over `image.tag` and renders an
immutable `repository@sha256:...` reference. `make test` verifies both the tag
fallback and digest override and rejects malformed digests.

Production release closure is stricter than the normal static suite. After the
live acceptance gates are green, pin the actual digest in all Kubernetes and
OpenShift values files and run:

```bash
REQUIRE_IMAGE_DIGEST=1 make published-image-contract
```

That networked check verifies the published image exists, carries source
revision provenance, uses the expected entrypoint, and does not mutate `/tmp`
or immutable `/etc`. `make index` packages charts into `repo/` while preserving
historical package archives and refusing to overwrite changed content at an
existing version.

## Publishing to GitHub Pages

GitHub Pages serves `master:/docs`. The chart index and archives are
available at [ngxstorage.github.io/helm](https://ngxstorage.github.io/helm/).

1. Give each changed chart a version unused in the published index.
2. Run `make test` and `REQUIRE_IMAGE_DIGEST=1 make published-image-contract`.
3. Run `make pages` to package into `repo/`, synchronize the feed under
   `docs/` and verify both directories. Existing package bytes are preserved.
4. Update the installation version in this README and `docs/index.html`.
   Commit the sources, indexes and new packages to `master`. After Pages
   deploys, pull each new chart from the public repository and render it.

## Sidecar images

Charts do not reference `registry.k8s.io` directly. All six upstream CSI
sidecars (provisioner, attacher, resizer, snapshotter, node-driver-registrar,
livenessprobe) are mirrored to `quay.io/ngxstorage/csi-sidecars/<image>:<tag>`
with the multi-arch manifest list preserved, and the chart defaults are
digest-pinned mirror references under `sidecars.*`. Override the block only
when mirroring to another registry; keep immutable digests in production.

## License

[Apache License 2.0](LICENSE)
