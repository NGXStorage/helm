#!/usr/bin/env bash
set -euo pipefail

helm_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="${REPO_DIR:-$helm_root/repo}"
repo_url="${REPO_URL:-https://ngxstorage.github.io/helm}"
staging_dir="$(mktemp -d)"
trap 'rm -rf "$staging_dir"' EXIT

charts=(
  "$helm_root/nfs/kubernetes"
  "$helm_root/nfs/openshift"
  "$helm_root/fc/kubernetes"
  "$helm_root/fc/openshift"
  "$helm_root/iscsi/kubernetes"
  "$helm_root/iscsi/openshift"
)

mkdir -p "$repo_dir"
for chart in "${charts[@]}"; do
  helm lint --strict "$chart"
  helm template ci "$chart" >/dev/null
  helm package "$chart" --destination "$staging_dir"
done

packages_have_same_content() {
  local existing="$1"
  local candidate="$2"
  local compare_root
  compare_root="$(mktemp -d "$staging_dir/compare.XXXXXX")"
  mkdir -p "$compare_root/existing" "$compare_root/candidate"
  tar -xzf "$existing" -C "$compare_root/existing"
  tar -xzf "$candidate" -C "$compare_root/candidate"
  diff -qr --no-dereference \
    "$compare_root/existing" "$compare_root/candidate" >/dev/null
}

for package in "$staging_dir"/*.tgz; do
  destination="$repo_dir/$(basename "$package")"
  # Helm embeds timestamps in the gzip/tar archive, so byte comparison would
  # reject a repeat package of unchanged sources. Compare extracted chart
  # contents instead and still fail closed when a published version changed.
  if [[ -f "$destination" ]] && ! packages_have_same_content "$destination" "$package"; then
    echo "Refusing to overwrite an existing chart version: $(basename "$package")" >&2
    echo "Bump version in Chart.yaml before publishing changed chart content." >&2
    exit 1
  fi
  if [[ ! -f "$destination" ]]; then
    cp "$package" "$destination"
  fi
done
helm repo index "$repo_dir" --url "$repo_url"
echo "Helm repository written to $repo_dir"
