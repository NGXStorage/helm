#!/usr/bin/env bash
set -euo pipefail

helm_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT

for protocol in fc iscsi nfs; do
  for platform in kubernetes openshift; do
    chart="$helm_root/$protocol/$platform"
    expected_container="/host/var/log/ngxstorage/$protocol"
    helm template "log-${protocol}-${platform}" "$chart" \
      --namespace ngxstorage-log-validation \
      --set-string driverNamePrefix=201 >"$rendered"

    ruby - "$rendered" "$protocol" "$expected_container" <<'RUBY'
require "yaml"

path, protocol, expected_container = ARGV
documents = YAML.load_stream(File.read(path)).compact
driver_name = "#{protocol}-csi-ngxstorage"

daemonset = documents.find { |doc| doc["kind"] == "DaemonSet" }
abort "#{protocol}: node DaemonSet missing" unless daemonset
node_spec = daemonset.dig("spec", "template", "spec")
driver = node_spec.fetch("containers").find { |container| container["name"] == driver_name }
abort "#{protocol}: node driver container missing" unless driver
expected_prefix_arg = "--driver-prefix=201"
abort "#{protocol}: node driver prefix missing" unless (driver["args"] || []).include?(expected_prefix_arg)

log_env = (driver["env"] || []).find { |entry| entry["name"] == "NGXSTORAGE_CSI_LOG_DIR" }
abort "#{protocol}: log env path mismatch: #{log_env.inspect}" unless log_env && log_env["value"] == expected_container

host_root = (driver["volumeMounts"] || []).find { |mount| mount["name"] == "host-root" }
abort "#{protocol}: /host root mount missing" unless host_root && host_root["mountPath"] == "/host"
if (driver["volumeMounts"] || []).any? { |mount| mount["name"] == "log-dir" }
  abort "#{protocol}: redundant log-dir mount must not be present"
end
if (node_spec["volumes"] || []).any? { |volume| volume["name"] == "log-dir" }
  abort "#{protocol}: redundant log-dir volume must not be present"
end

deployment = documents.find { |doc| doc["kind"] == "Deployment" }
abort "#{protocol}: controller Deployment missing" unless deployment
controller = deployment.dig("spec", "template", "spec", "containers").find { |container| container["name"] == driver_name }
abort "#{protocol}: controller driver container missing" unless controller
abort "#{protocol}: controller driver prefix missing" unless (controller["args"] || []).include?(expected_prefix_arg)
if (controller["env"] || []).any? { |entry| entry["name"] == "NGXSTORAGE_CSI_LOG_DIR" }
  abort "#{protocol}: controller must remain stdout-only"
end
if (controller["volumeMounts"] || []).any? { |mount| mount["mountPath"].to_s.start_with?("/host/var/log/ngxstorage") }
  abort "#{protocol}: controller must not mount host log storage"
end

csi_driver = documents.find { |doc| doc["kind"] == "CSIDriver" }
expected_driver_name = "201.#{protocol}.csi.ngxstorage.com"
unless csi_driver && csi_driver.dig("metadata", "name") == expected_driver_name
  abort "#{protocol}: prefixed CSIDriver name mismatch: #{csi_driver&.dig("metadata", "name").inspect}"
end
RUBY
    echo "$protocol/$platform: log path contract passed."
  done
done
