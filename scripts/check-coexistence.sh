#!/usr/bin/env bash
set -euo pipefail

# Validate combinations that can actually be installed together. The
# Kubernetes and OpenShift charts for one protocol intentionally advertise the
# same CSI identity, so comparing all six renders together is a false
# collision. Each platform group is checked independently.
helm_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rendered_dir="$(mktemp -d)"
trap 'rm -rf "$rendered_dir"' EXIT

cluster_scoped_kinds='^(CSIDriver|StorageClass|VolumeSnapshotClass|ClusterRole|ClusterRoleBinding|SecurityContextConstraints)$'

render_group() {
  local platform="$1"
  shift
  local output="$rendered_dir/$platform.yaml"
  : >"$output"

  local chart
  for chart in "$@"; do
    echo "Rendering $chart ($platform coexistence group)" >&2
    # Namespace is a Helm release option, not a chart value.
    helm template "coexist-${platform}-${chart%%/*}" "$helm_root/$chart" \
      --namespace "ngxstorage-coexistence-${platform}" >>"$output"
  done

  printf '%s\n' "$output"
}

check_cluster_scoped_names() {
  local platform="$1"
  local file="$2"
  local duplicates
  duplicates="$(awk -v kinds="$cluster_scoped_kinds" '
    function reset() { kind=""; inmeta=0 }
    /^---[[:space:]]*$/ { reset(); next }
    /^kind:[[:space:]]*/ { kind=$2; inmeta=0; next }
    /^metadata:[[:space:]]*$/ { inmeta=1; next }
    inmeta && /^  name:[[:space:]]*/ {
      name=$2
      if (kind ~ kinds) print kind "/" name
      inmeta=0
    }
  ' "$file" | sort | uniq -d || true)"

  if [[ -n "$duplicates" ]]; then
    echo "$platform: cluster-scoped resource collisions detected:" >&2
    echo "$duplicates" >&2
    return 1
  fi
}

check_csi_names() {
  local platform="$1"
  local file="$2"
  local names
  names="$(awk '
    function reset() { kind=""; inmeta=0 }
    /^---[[:space:]]*$/ { reset(); next }
    /^kind:[[:space:]]*/ { kind=$2; inmeta=0; next }
    /^metadata:[[:space:]]*$/ { inmeta=1; next }
    inmeta && /^  name:[[:space:]]*/ {
      if (kind == "CSIDriver") print $2
      inmeta=0
    }
  ' "$file")"

  if [[ "$(printf '%s\n' "$names" | sed '/^$/d' | sort -u | wc -l)" -ne 3 ]]; then
    echo "$platform: expected three unique CSIDriver names, got:" >&2
    printf '%s\n' "$names" >&2
    return 1
  fi
  if [[ "$(printf '%s\n' "$names" | sed '/^$/d' | sort | uniq -d)" ]]; then
    echo "$platform: duplicate CSIDriver names detected:" >&2
    printf '%s\n' "$names" | sort | uniq -d >&2
    return 1
  fi
}

check_node_contracts() {
  local platform="$1"
  local file="$2"
  local records="$rendered_dir/${platform}-nodes.tsv"

  # Emit one record for each hostNetwork node DaemonSet. Registration and
  # hostPath socket paths must agree, and every protocol must own a distinct
  # host liveness port because all node pods bind on 0.0.0.0.
  awk '
    function reset() {
      kind=""; name=""; hostnetwork=0; registration=""; socket=""; port=""
    }
    function report() {
      if (kind == "DaemonSet" && hostnetwork)
        print name "\t" registration "\t" socket "\t" port
    }
    BEGIN { reset() }
    /^---[[:space:]]*$/ { report(); reset(); next }
    /^kind:[[:space:]]*/ { kind=$2; next }
    /^  name:[[:space:]]*/ && name == "" { name=$2; next }
    /^[[:space:]]+hostNetwork:[[:space:]]*true[[:space:]]*$/ { hostnetwork=1; next }
    /--kubelet-registration-path=\/var\/lib\/kubelet\/plugins\// {
      value=$0
      sub(/^.*plugins\//, "", value)
      sub(/\/csi\.sock.*$/, "", value)
      registration=value
      next
    }
    /--http-endpoint=0\.0\.0\.0:/ {
      value=$0
      sub(/^.*0\.0\.0\.0:/, "", value)
      sub(/[[:space:]]*$/, "", value)
      port=value
      next
    }
    /^[[:space:]]+path:[[:space:]]*\/var\/lib\/kubelet\/plugins\// {
      value=$0
      sub(/^.*plugins\//, "", value)
      sub(/[[:space:]]*$/, "", value)
      socket=value
      next
    }
    END { report() }
  ' "$file" >"$records"

  if [[ ! -s "$records" ]]; then
    echo "$platform: no hostNetwork node DaemonSet found" >&2
    return 1
  fi

  local node_name registration socket port expected
  local -a ports=()
  while IFS=$'\t' read -r node_name registration socket port; do
    if [[ -z "$registration" || -z "$socket" || -z "$port" ]]; then
      echo "$platform: incomplete node registration/socket/liveness contract for $node_name" >&2
      return 1
    fi
    if [[ "$registration" != "$socket" ]]; then
      echo "$platform: registration path and socket path differ for $node_name" >&2
      printf 'registration=%s socket=%s\n' "$registration" "$socket" >&2
      return 1
    fi
    case "$node_name" in
      fc-*) expected=9811 ;;
      iscsi-*) expected=9812 ;;
      nfs-*) expected=9809 ;;
      *) echo "$platform: unknown hostNetwork node DaemonSet $node_name" >&2; return 1 ;;
    esac
    if [[ "$port" != "$expected" ]]; then
      echo "$platform: $node_name must expose node liveness port $expected (got $port)" >&2
      return 1
    fi
    ports+=("$port")
  done <"$records"

  if [[ "${#ports[@]}" -ne 3 ]]; then
    echo "$platform: expected exactly three hostNetwork node DaemonSets" >&2
    return 1
  fi
  if [[ "$(printf '%s\n' "${ports[@]}" | sort | uniq -d)" ]]; then
    echo "$platform: duplicate hostNetwork node liveness ports detected:" >&2
    printf '%s\n' "${ports[@]}" | sort | uniq -d >&2
    return 1
  fi
  if [[ "$(cut -f2 "$records" | sort | uniq -d)" ]]; then
    echo "$platform: duplicate CSI registration paths detected:" >&2
    cut -f2 "$records" | sort | uniq -d >&2
    return 1
  fi
  if [[ "$(cut -f3 "$records" | sort | uniq -d)" ]]; then
    echo "$platform: duplicate CSI socket paths detected:" >&2
    cut -f3 "$records" | sort | uniq -d >&2
    return 1
  fi
}

check_group() {
  local platform="$1"
  local file="$2"
  check_cluster_scoped_names "$platform" "$file"
  check_csi_names "$platform" "$file"
  check_node_contracts "$platform" "$file"
  echo "$platform: FC, iSCSI, and NFS coexistence checks passed."
}

check_prefixed_pair() {
  local protocol="$1"
  local platform="$2"
  local output="$rendered_dir/${protocol}-${platform}-prefixed-pair.yaml"
  : >"$output"

  local prefix
  for prefix in 201 205; do
    helm template "${protocol}-${platform}-${prefix}" "$helm_root/$protocol/$platform" \
      --namespace "ngxstorage-${protocol}-${platform}-pair" \
      --set-string "driverNamePrefix=$prefix" >>"$output"
  done

  ruby - "$output" "$protocol" <<'RUBY'
require "yaml"

path, protocol = ARGV
documents = YAML.load_stream(File.read(path)).compact
identities = documents.map do |document|
  [document["kind"], document.dig("metadata", "namespace"), document.dig("metadata", "name")]
end
duplicates = identities.group_by(&:itself).select { |_identity, occurrences| occurrences.length > 1 }.keys
abort "#{protocol}: prefixed instances have resource collisions: #{duplicates.inspect}" unless duplicates.empty?

csi_names = documents.filter_map do |document|
  document.dig("metadata", "name") if document["kind"] == "CSIDriver"
end.sort
expected = ["201.#{protocol}.csi.ngxstorage.com", "205.#{protocol}.csi.ngxstorage.com"]
abort "#{protocol}: prefixed CSIDriver identities mismatch: #{csi_names.inspect}" unless csi_names == expected
RUBY
  echo "$protocol/$platform: prefixed 201/205 coexistence passed."
}

kubernetes_rendered="$(render_group kubernetes nfs/kubernetes fc/kubernetes iscsi/kubernetes)"
openshift_rendered="$(render_group openshift nfs/openshift fc/openshift iscsi/openshift)"
check_group kubernetes "$kubernetes_rendered"
check_group openshift "$openshift_rendered"

for protocol in fc iscsi nfs; do
  check_prefixed_pair "$protocol" kubernetes
  check_prefixed_pair "$protocol" openshift
done
