#!/usr/bin/env bash
set -euo pipefail

helm_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
charts=(
  fc/kubernetes fc/openshift
  iscsi/kubernetes iscsi/openshift
  nfs/kubernetes nfs/openshift
)

validator="${KUBECONFORM_BIN:-}"
if [[ -z "$validator" ]] && command -v kubeconform >/dev/null 2>&1; then
  validator="$(command -v kubeconform)"
fi

rendered_dir="$(mktemp -d)"
trap 'rm -rf "$rendered_dir"' EXIT

for chart in "${charts[@]}"; do
  output="$rendered_dir/${chart//\//-}.yaml"
  echo "Rendering $chart"
  helm template "validation-${chart//\//-}" "$helm_root/$chart" \
    --namespace ngxstorage-validation >"$output"

  if [[ -n "$validator" ]]; then
    "$validator" -strict -summary -ignore-missing-schemas "$output"
  else
    # Keep local `make test` useful on developer machines without kubeconform.
    # CI installs kubeconform and therefore performs full Kubernetes schema
    # validation there. Ruby's YAML parser still catches malformed documents.
    ruby -e 'require "yaml"; YAML.parse_stream(File.read(ARGV.fetch(0)))' "$output"
  fi
done

if [[ -z "$validator" ]]; then
  echo "kubeconform not found; YAML syntax validation passed (CI runs strict schema validation)."
fi
