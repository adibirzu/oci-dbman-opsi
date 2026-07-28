#!/usr/bin/env bash
# Demo/PoC only. Runs one reviewed command on the disposable private jump host.
set -euo pipefail

PROFILE="${PROFILE:?set PROFILE}"
REGION="${REGION:?set REGION}"
COMPARTMENT_ID="${COMPARTMENT_ID:?set COMPARTMENT_ID}"
LIFECYCLE_ID="${LIFECYCLE_ID:?set LIFECYCLE_ID from the disposable deployment}"
BASTION_NAME="${BASTION_NAME:-dbman-opsi-disposable-bastion}"
JUMPHOST_NAME="${JUMPHOST_NAME:-dbman-opsi-disposable-jumphost}"
HOST_KEY="${HOST_KEY:?set HOST_KEY to the jump-host private key path}"
LOCAL_PORT="${LOCAL_PORT:-2223}"

[ "$#" -gt 0 ] || { echo "Usage: $0 <reviewed-command> [args...]" >&2; exit 2; }

tmp="$(mktemp -d "${TMPDIR:-/tmp}/dbman-opsi-bastion.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT
ssh-keygen -q -t ed25519 -N '' -f "$tmp/session-key"

[[ "$BASTION_NAME" =~ ^[A-Za-z0-9._-]{1,255}$ ]] || { echo "BASTION_NAME contains unsupported characters" >&2; exit 2; }
[[ "$JUMPHOST_NAME" =~ ^[A-Za-z0-9._-]{1,255}$ ]] || { echo "JUMPHOST_NAME contains unsupported characters" >&2; exit 2; }
[[ "$LIFECYCLE_ID" =~ ^[A-Za-z0-9._-]{1,255}$ ]] || { echo "LIFECYCLE_ID contains unsupported characters" >&2; exit 2; }

oci_call() {
  local config_args=()
  if [ -n "${OCI_CLI_CONFIG_FILE:-}" ]; then
    config_args=(--config-file "$OCI_CLI_CONFIG_FILE")
  fi
  oci --profile "$PROFILE" --region "$REGION" "${config_args[@]}" "$@"
}

resolve_id() {
  local kind="$1" name="$2" query="$3" value
  shift 3
  for _ in {1..6}; do
    value="$(oci_call "$@" --query "$query" --raw-output 2>/dev/null || true)"
    if [ -n "$value" ] && [ "$value" != "null" ]; then
      printf '%s' "$value"
      return 0
    fi
    sleep 5
  done
  echo "Unable to resolve active $kind named $name after retries" >&2
  return 1
}

bastion_query="data[?name==\`$BASTION_NAME\` && \"freeform-tags\".dbman_opsi_lifecycle==\`$LIFECYCLE_ID\`].id | [0]"
instance_query="data[?\"display-name\"==\`$JUMPHOST_NAME\` && \"freeform-tags\".dbman_opsi_lifecycle==\`$LIFECYCLE_ID\`].id | [0]"
bastion_id="$(resolve_id bastion "$BASTION_NAME" "$bastion_query" bastion bastion list --compartment-id "$COMPARTMENT_ID" --all)"
instance_id="$(resolve_id jump-host "$JUMPHOST_NAME" "$instance_query" compute instance list --compartment-id "$COMPARTMENT_ID" --all)"
vnic_id="$(oci_call compute vnic-attachment list --compartment-id "$COMPARTMENT_ID" --instance-id "$instance_id" --query 'data[0]."vnic-id"' --raw-output)"
private_ip="$(oci_call network vnic get --vnic-id "$vnic_id" --query 'data."private-ip"' --raw-output)"

session_json="$tmp/session.json"
oci_call bastion session create-port-forwarding --bastion-id "$bastion_id" --display-name "dbman-opsi-ephemeral-run" --target-resource-id "$instance_id" --target-private-ip "$private_ip" --target-port 22 --ssh-public-key-file "$tmp/session-key.pub" --key-type PUB --session-ttl 1800 --output json > "$session_json"
session_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["id"])' "$session_json")"

for _ in {1..12}; do
  state="$(oci_call bastion session get --session-id "$session_id" --query 'data."lifecycle-state"' --raw-output)"
  [ "$state" = ACTIVE ] && break
  [ "$state" = FAILED ] || [ "$state" = DELETED ] && { echo "Bastion session failed: $state" >&2; exit 1; }
  sleep 5
done
[ "${state:-}" = ACTIVE ] || { echo "Bastion session did not become ACTIVE" >&2; exit 1; }

command="$(oci_call bastion session get --session-id "$session_id" --query 'data."ssh-metadata".command' --raw-output)"
command="${command/<privateKey>/$tmp/session-key}"
command="${command/<localPort>/$LOCAL_PORT}"
command="${command/ssh /ssh -f -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$tmp/known_hosts }"
bash -c "$command"
ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$tmp/jumphost_known_hosts" -o ConnectTimeout=15 -i "$HOST_KEY" -p "$LOCAL_PORT" opc@127.0.0.1 -- "$@"
