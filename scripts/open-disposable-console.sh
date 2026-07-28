#!/usr/bin/env bash
# Demo/PoC only: serial-console recovery for the disposable jump host.
set -euo pipefail

PROFILE="${PROFILE:?set PROFILE}"
REGION="${REGION:?set REGION}"
COMPARTMENT_ID="${COMPARTMENT_ID:?set COMPARTMENT_ID}"
LIFECYCLE_ID="${LIFECYCLE_ID:?set LIFECYCLE_ID from the disposable deployment}"
JUMPHOST_NAME="${JUMPHOST_NAME:-dbman-opsi-disposable-jumphost}"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/dbman-opsi-console.XXXXXX")"
connection_id=""

[[ "$JUMPHOST_NAME" =~ ^[A-Za-z0-9._-]{1,255}$ ]] || { echo "JUMPHOST_NAME contains unsupported characters" >&2; exit 2; }
[[ "$LIFECYCLE_ID" =~ ^[A-Za-z0-9._-]{1,255}$ ]] || { echo "LIFECYCLE_ID contains unsupported characters" >&2; exit 2; }

oci_call() {
  local config_args=()
  if [ -n "${OCI_CLI_CONFIG_FILE:-}" ]; then
    config_args=(--config-file "$OCI_CLI_CONFIG_FILE")
  fi
  oci --profile "$PROFILE" --region "$REGION" "${config_args[@]}" "$@"
}

cleanup() {
  if [ -n "$connection_id" ]; then
    oci_call compute instance-console-connection delete --instance-console-connection-id "$connection_id" --force >/dev/null 2>&1 || true
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT

# Instance Console Connections require RSA in OCI environments that reject
# Ed25519 serial-console keys. The key exists only for this invocation.
ssh-keygen -q -t rsa -b 4096 -N '' -f "$tmp/console-key"
instance_query="data[?\"display-name\"==\`$JUMPHOST_NAME\` && \"freeform-tags\".dbman_opsi_lifecycle==\`$LIFECYCLE_ID\`].id | [0]"
instance_id="$(oci_call compute instance list --compartment-id "$COMPARTMENT_ID" --all --query "$instance_query" --raw-output)"
[ -n "$instance_id" ] && [ "$instance_id" != "null" ] || { echo "No lifecycle-tagged disposable jump host found" >&2; exit 1; }
connection_json="$tmp/connection.json"
oci_call compute instance-console-connection create --instance-id "$instance_id" --ssh-public-key-file "$tmp/console-key.pub" --wait-for-state ACTIVE --max-wait-seconds 600 --wait-interval-seconds 15 --output json > "$connection_json"
connection_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["id"])' "$connection_json")"
connection_string="$(oci_call compute instance-console-connection get --instance-console-connection-id "$connection_id" --query 'data."connection-string"' --raw-output)"

echo "Console connection is ACTIVE. Starting serial console; exit the SSH session to close it."
connection_string="${connection_string/<privateKey>/$tmp/console-key}"
bash -c "$connection_string"
