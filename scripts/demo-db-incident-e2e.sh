#!/usr/bin/env bash
set -euo pipefail

PROFILE="${PROFILE:-${OCI_PROFILE:-}}"
REGION="${REGION:-${OCI_REGION:-}}"
CONFIG="${CONFIG:-${DBMAN_OPSI_CONFIG:-dbman-opsi.local.yaml}}"
OUTPUT_DIR="${OUTPUT_DIR:-generated/db-incident-demo-e2e}"
SCENARIO_ID="${SCENARIO_ID:-}"
DATABASE_NAME="${DATABASE_NAME:-${DEMO_DATABASE_NAME:-}}"
OCI_BIN="${OCI_BIN:-oci}"
PYTHON_BIN="${PYTHON_BIN:-python}"

if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
  BOLD="$(printf '\033[1m')"
  GREEN="$(printf '\033[32m')"
  YELLOW="$(printf '\033[33m')"
  RED="$(printf '\033[31m')"
  BLUE="$(printf '\033[34m')"
  RESET="$(printf '\033[0m')"
else
  BOLD=""
  GREEN=""
  YELLOW=""
  RED=""
  BLUE=""
  RESET=""
fi

step() { printf '\n%s%s%s\n' "$BOLD" "$1" "$RESET"; }
ok() { printf '%sOK%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%sWARN%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail() { printf '%sFAIL%s %s\n' "$RED" "$RESET" "$1"; }
info() { printf '%sINFO%s %s\n' "$BLUE" "$RESET" "$1"; }

redact_remote_output() {
  if [ -n "${DEMO_JUMPHOST_HOST:-}" ]; then
    sed "s/${DEMO_JUMPHOST_HOST}/<DEMO_JUMPHOST_HOST>/g"
  else
    cat
  fi
}

usage() {
  cat <<'USAGE'
Usage: scripts/demo-db-incident-e2e.sh <command>

Commands:
  prereq          Check local, OCI, DBM/OPSI/Log Analytics, and secret prerequisites.
  generate        Generate the DB incident demo packet.
  package         Create a tarball that can be copied to the demo jumphost or DB host.
  jumphost-copy   Copy the generated packet tarball to a direct SSH demo jumphost.
  jumphost-probe  Probe SSH access for DEMO_JUMPHOST_NAME/DEMO_JUMPHOST_CANDIDATES.
  jumphost-preflight
                  Run packet-local tooling preflight on the demo jumphost.
  jumphost-run    Run the real DB workload on the demo jumphost using env secrets.
  logan-scenario-check
                  Query Log Analytics for the generated scenario_id/lab_id.
  wait-db         Poll a disposable DBCS target until AVAILABLE and print the next deployment steps.
  bastion-plan    Print OCI Bastion/jumphost execution commands with placeholders.
  logan-check     Run a live Log Analytics + DBM + OPSI + Data Safe evidence query.
  tasks           Print the full end-to-end task checklist.

Required for real DB workload execution:
  PROFILE or OCI_PROFILE       OCI CLI profile name.
  REGION or OCI_REGION         OCI region.
  CONFIG or DBMAN_OPSI_CONFIG  Local ignored dbman-opsi config path.
  DB_INCIDENT_ADMIN_CONNECT   SQL*Plus/SQLcl admin connect string for the demo DB/PDB.
  DB_INCIDENT_LAB_PASSWORD    Disposable DBINC_LAB password.
  SQLcl or sqlplus on the execution host.

Optional for demo jumphost path:
  DEMO_JUMPHOST_NAME           Preferred compute instance display name.
  DEMO_JUMPHOST_CANDIDATES     Comma-separated names to probe.
  DEMO_JUMPHOST_HOST           SSH hostname/IP for direct jumphost SSH.
  DEMO_JUMPHOST_USER           SSH user, default opc.
  DEMO_JUMPHOST_PORT           SSH port, default 22.
  DEMO_JUMPHOST_SSH_KEY        Private key path for direct jumphost SSH.
  DEMO_JUMPHOST_KNOWN_HOSTS    Writable known_hosts file path for SSH/SCP.
  DEMO_JUMPHOST_REMOTE_DIR     Remote directory, default /tmp/db-incident-demo-e2e.
  DEMO_JUMPHOST_RUN_AS_ORACLE  Set true to run the packet as the oracle OS user via sudo.
  DEMO_JUMPHOST_PREFER_PRIVATE Set true to prefer private IP when resolving by name.
  DEMO_DB_PRIVATE_IP           DB host private IP when using OCI Bastion port-forward.
  DEMO_BASTION_NAME            OCI Bastion name.
  DEMO_DB_SSH_KEY              Private key path for OCI Bastion session; .pub must exist.

Optional for wait-db:
  DB_SYSTEM_NAME                Disposable DBCS display name (defaults to DEMO_DATABASE_NAME).
  DB_WAIT_SECONDS               Poll interval in seconds (default 60).
  DB_WAIT_MAX_MINUTES           Maximum wait before returning a non-zero status (default 90).
USAGE
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_value() {
  value="$1"
  label="$2"
  [ -n "$value" ] || { fail "$label is required"; exit 2; }
}

require_oci_env() {
  if [ -f "$CONFIG" ]; then
    configured_profile="$(config_value profile)"
    configured_region="$(config_value region)"
    PROFILE="${PROFILE:-$configured_profile}"
    REGION="${REGION:-$configured_region}"
  fi
  require_value "$PROFILE" "PROFILE or OCI_PROFILE"
  require_value "$REGION" "REGION or OCI_REGION"
  if [ -f "$CONFIG" ]; then
    configured_region="$(config_value region)"
    if [ -n "$configured_region" ] && [ "$configured_region" != "$REGION" ]; then
      fail "selected REGION ($REGION) does not match config region ($configured_region)"
      info "Use the selected deployment region, or pass the matching config file. No OCI changes were made."
      exit 2
    fi
  fi
  export PROFILE REGION
}

config_value() {
  "$PYTHON_BIN" - "$CONFIG" "$1" <<'PY'
import sys, yaml
path, key = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
value = data
for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
print("" if value is None else value)
PY
}

first_target_name() {
  "$PYTHON_BIN" - "$CONFIG" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as handle:
    data = yaml.safe_load(handle) or {}
targets = data.get("targets") or []
if isinstance(targets, list) and targets:
    first = targets[0] or {}
    print(first.get("name", ""))
PY
}

compartment_id() {
  config_value "compartment_id"
}

namespace() {
  require_oci_env
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" os ns get --query data --raw-output
}

resolve_scenario_id() {
  if [ -n "$SCENARIO_ID" ]; then
    return
  fi
  if [ -f "$OUTPUT_DIR/manifest.json" ]; then
    SCENARIO_ID="$("$PYTHON_BIN" - "$OUTPUT_DIR/manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = (json.load(handle) or {}).get("scenario_id", "")
print(value if isinstance(value, str) else "")
PY
)"
  fi
  SCENARIO_ID="${SCENARIO_ID:-dbinc-$(date -u +%Y%m%d%H%M%S)}"
  export SCENARIO_ID
}

generate_packet() {
  resolve_scenario_id
  step "Generating DB incident demo packet"
  PYTHONPATH=src "$PYTHON_BIN" -m dbman_opsi.cli generate-db-incident-demo \
    --output "$OUTPUT_DIR" \
    --apply \
    --scenario-id "$SCENARIO_ID"
  "$OUTPUT_DIR/validate-demo-packet.sh"
  ok "packet generated at $OUTPUT_DIR"
}

check_prereq() {
  step "Local tools"
  command_exists "$OCI_BIN" && ok "OCI CLI found" || fail "OCI CLI not found"
  command_exists "$PYTHON_BIN" && ok "Python found" || fail "Python not found"
  if command_exists sqlplus; then
    ok "sqlplus found"
  elif command_exists sql; then
    ok "SQLcl found"
  else
    warn "sqlplus/SQLcl not found locally; run the workload on the demo jumphost or DB host."
  fi

  require_oci_env
  step "Local config"
  [ -f "$CONFIG" ] && ok "config found: $CONFIG" || fail "config missing: $CONFIG"
  comp="$(compartment_id)"
  [ -n "$comp" ] && ok "compartment_id resolved from config" || fail "compartment_id missing in config"

  step "OCI session and services"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" iam region-subscription list >/dev/null
  ok "OCI profile is authenticated"

  ns="$(namespace)"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics namespace get --namespace-name "$ns" >/dev/null
  ok "Log Analytics namespace is onboarded"

  log_groups="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics log-group list --namespace-name "$ns" --compartment-id "$comp" --all --query 'length(data.items)' --raw-output)"
  [ "${log_groups:-0}" -gt 0 ] && ok "Log Analytics log group exists in demo compartment" || warn "no Log Analytics log group found"

  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" database-management managed-database list --compartment-id "$comp" >/dev/null
  ok "Database Management list works"

  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" opsi database-insights list --compartment-id "$comp" --all >/dev/null
  ok "OPSI database insight list works"

  if "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe target-database list --compartment-id "$comp" >/dev/null 2>&1; then
    ok "Data Safe target list works"
  else
    warn "Data Safe target list is unavailable for this profile/compartment"
  fi

  step "Real workload secrets"
  [ -n "${DB_INCIDENT_ADMIN_CONNECT:-}" ] && ok "DB_INCIDENT_ADMIN_CONNECT is set" || warn "DB_INCIDENT_ADMIN_CONNECT is not set"
  [ -n "${DB_INCIDENT_LAB_PASSWORD:-}" ] && ok "DB_INCIDENT_LAB_PASSWORD is set" || warn "DB_INCIDENT_LAB_PASSWORD is not set"

  step "Demo jumphost / Bastion hints"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" bastion bastion list --compartment-id "$comp" --all \
    --query 'data[].{name:name,state:"lifecycle-state"}' --output table || true
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" search resource structured-search \
    --query-text "query instance resources where displayName =~ '.*jump.*' || displayName =~ '.*bastion.*' || displayName =~ '.*management.*'" \
    --limit 20 --query 'data.items[].{name:"display-name",state:"lifecycle-state"}' --output table || true
}

package_packet() {
  [ -d "$OUTPUT_DIR" ] || generate_packet
  tarball="${OUTPUT_DIR%/}.tgz"
  COPYFILE_DISABLE=1 tar --no-xattrs --exclude='.tools' --exclude='._*' -C "$(dirname "$OUTPUT_DIR")" -czf "$tarball" "$(basename "$OUTPUT_DIR")"
  ok "created $tarball"
  info "packet-local .tools caches are excluded; install SQLcl through the checksum-verified remote preflight when needed."
}

remote_dir() {
  printf '%s\n' "${DEMO_JUMPHOST_REMOTE_DIR:-/tmp/db-incident-demo-e2e}"
}

shell_quote() {
  printf '%q' "$1"
}

require_jumphost_env() {
  if [ -z "${DEMO_JUMPHOST_HOST:-}" ]; then
    resolved_host="$(resolve_jumphost_host)"
    if [ -n "$resolved_host" ]; then
      DEMO_JUMPHOST_HOST="$resolved_host"
      export DEMO_JUMPHOST_HOST
      ok "resolved DEMO_JUMPHOST_HOST from DEMO_JUMPHOST_NAME"
    fi
  fi
  [ -n "${DEMO_JUMPHOST_HOST:-}" ] || { fail "DEMO_JUMPHOST_HOST is required"; exit 2; }
  [ -n "${DEMO_JUMPHOST_SSH_KEY:-}" ] || { fail "DEMO_JUMPHOST_SSH_KEY is required"; exit 2; }
  [ -f "$DEMO_JUMPHOST_SSH_KEY" ] || { fail "DEMO_JUMPHOST_SSH_KEY file not found"; exit 2; }
}

resolve_jumphost_host() {
  require_oci_env
  [ -n "${DEMO_JUMPHOST_NAME:-}" ] || return 0
  name="$DEMO_JUMPHOST_NAME"
  "$PYTHON_BIN" - "$OCI_BIN" "$PROFILE" "$REGION" "$name" "${DEMO_JUMPHOST_PREFER_PRIVATE:-false}" <<'PY'
import json
import subprocess
import sys

oci_bin, profile, region, name, prefer_private = sys.argv[1:6]

def run_json(args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

items = run_json([
    oci_bin, "--profile", profile, "--region", region,
    "search", "resource", "structured-search",
    "--query-text", f"query instance resources where displayName = '{name}'",
    "--limit", "1",
    "--output", "json",
])
try:
    item = items["data"]["items"][0]
except (TypeError, KeyError, IndexError):
    sys.exit(0)

vnics = run_json([
    oci_bin, "--profile", profile, "--region", region,
    "compute", "instance", "list-vnics",
    "--compartment-id", item["compartment-id"],
    "--instance-id", item["identifier"],
    "--all",
    "--output", "json",
])
try:
    vnic = vnics["data"][0]
except (TypeError, KeyError, IndexError):
    sys.exit(0)

if prefer_private.lower() == "true":
    print(vnic.get("private-ip") or vnic.get("public-ip") or "")
else:
    print(vnic.get("public-ip") or vnic.get("private-ip") or "")
PY
}

ssh_base() {
  require_jumphost_env
  output="$(
    ssh -i "$DEMO_JUMPHOST_SSH_KEY" \
    -p "${DEMO_JUMPHOST_PORT:-22}" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${DEMO_JUMPHOST_KNOWN_HOSTS:-$HOME/.ssh/known_hosts}" \
    -o ConnectTimeout=20 \
      "${DEMO_JUMPHOST_USER:-opc}@${DEMO_JUMPHOST_HOST}" "$@" 2>&1
  )" || {
    code=$?
    printf '%s\n' "$output" | redact_remote_output >&2
    return "$code"
  }
  printf '%s\n' "$output" | redact_remote_output
}

ssh_stdin() {
  require_jumphost_env
  ssh -i "$DEMO_JUMPHOST_SSH_KEY" \
    -p "${DEMO_JUMPHOST_PORT:-22}" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${DEMO_JUMPHOST_KNOWN_HOSTS:-$HOME/.ssh/known_hosts}" \
    -o ConnectTimeout=20 \
    "${DEMO_JUMPHOST_USER:-opc}@${DEMO_JUMPHOST_HOST}" "bash -s"
}

jumphost_copy() {
  package_packet
  require_jumphost_env
  rd="$(remote_dir)"
  tarball="${OUTPUT_DIR%/}.tgz"
  step "Copying packet to demo jumphost"
  ssh_base "mkdir -p '$rd'"
  output="$(
    scp -i "$DEMO_JUMPHOST_SSH_KEY" \
    -P "${DEMO_JUMPHOST_PORT:-22}" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${DEMO_JUMPHOST_KNOWN_HOSTS:-$HOME/.ssh/known_hosts}" \
    -o ConnectTimeout=20 \
      "$tarball" "${DEMO_JUMPHOST_USER:-opc}@${DEMO_JUMPHOST_HOST}:$rd/" 2>&1
  )" || {
    code=$?
    printf '%s\n' "$output" | redact_remote_output >&2
    return "$code"
  }
  printf '%s\n' "$output" | redact_remote_output
  ssh_base "sudo -n rm -rf '$rd/$(basename "$OUTPUT_DIR")' && cd '$rd' && tar --no-same-owner --no-same-permissions -xzf '$(basename "$tarball")'"
  ok "packet copied to demo jumphost:$rd/$(basename "$OUTPUT_DIR")"
}

jumphost_probe() {
  [ -n "${DEMO_JUMPHOST_SSH_KEY:-}" ] || { fail "DEMO_JUMPHOST_SSH_KEY is required"; exit 2; }
  [ -f "$DEMO_JUMPHOST_SSH_KEY" ] || { fail "DEMO_JUMPHOST_SSH_KEY file not found"; exit 2; }
  candidates="${DEMO_JUMPHOST_CANDIDATES:-${DEMO_JUMPHOST_NAME:-}}"
  [ -n "$candidates" ] || { fail "DEMO_JUMPHOST_CANDIDATES or DEMO_JUMPHOST_NAME is required"; exit 2; }
  step "Probing demo jumphost SSH access"
  old_host="${DEMO_JUMPHOST_HOST:-}"
  old_name="${DEMO_JUMPHOST_NAME:-}"
  found=""
  IFS=',' read -r -a names <<< "$candidates"
  for candidate in "${names[@]}"; do
    DEMO_JUMPHOST_NAME="$(printf '%s' "$candidate" | xargs)"
    DEMO_JUMPHOST_HOST=""
    export DEMO_JUMPHOST_NAME DEMO_JUMPHOST_HOST
    resolved_host="$(resolve_jumphost_host)"
    if [ -z "$resolved_host" ]; then
      warn "$DEMO_JUMPHOST_NAME: not found"
      continue
    fi
    DEMO_JUMPHOST_HOST="$resolved_host"
    export DEMO_JUMPHOST_HOST
    probe_err="$(mktemp -t db-incident-ssh-probe.XXXXXX)"
    if ssh_base "true" >/dev/null 2>"$probe_err"; then
      ok "$DEMO_JUMPHOST_NAME: SSH reachable"
      found="$DEMO_JUMPHOST_NAME"
      rm -f "$probe_err"
      break
    fi
    redact_remote_output <"$probe_err" >&2 || true
    rm -f "$probe_err"
    warn "$DEMO_JUMPHOST_NAME: SSH not reachable with provided key/user"
  done
  DEMO_JUMPHOST_HOST="$old_host"
  DEMO_JUMPHOST_NAME="$old_name"
  export DEMO_JUMPHOST_HOST DEMO_JUMPHOST_NAME
  [ -n "$found" ] || { fail "no demo jumphost candidate accepted the provided SSH key/user"; exit 1; }
}

jumphost_preflight() {
  require_jumphost_env
  rd="$(remote_dir)"
  q_tooling_install="$(shell_quote "${DB_INCIDENT_TOOLING_INSTALL:-false}")"
  q_sqlcl_url="$(shell_quote "${DB_INCIDENT_SQLCL_URL:-}")"
  q_sqlcl_sha256="$(shell_quote "${DB_INCIDENT_SQLCL_SHA256:-}")"
  step "Running demo jumphost preflight"
  ssh_base "cd '$rd/$(basename "$OUTPUT_DIR")' && DB_INCIDENT_TOOLING_INSTALL=$q_tooling_install DB_INCIDENT_SQLCL_URL=$q_sqlcl_url DB_INCIDENT_SQLCL_SHA256=$q_sqlcl_sha256 ./08-local-demo-tooling-preflight.sh && ./validate-demo-packet.sh"
}

jumphost_run() {
  require_jumphost_env
  [ -n "${DB_INCIDENT_ADMIN_CONNECT:-}" ] || { fail "DB_INCIDENT_ADMIN_CONNECT is required"; exit 2; }
  [ -n "${DB_INCIDENT_LAB_PASSWORD:-}" ] || { fail "DB_INCIDENT_LAB_PASSWORD is required"; exit 2; }
  rd="$(remote_dir)"
  remote_packet_dir="$rd/$(basename "$OUTPUT_DIR")"
  q_admin="$(shell_quote "$DB_INCIDENT_ADMIN_CONNECT")"
  q_lab_password="$(shell_quote "$DB_INCIDENT_LAB_PASSWORD")"
  q_lab_connect="$(shell_quote "${DB_INCIDENT_LAB_CONNECT:-}")"
  q_lab_ezconnect="$(shell_quote "${DB_INCIDENT_LAB_EZCONNECT:-}")"
  q_pdb_name="$(shell_quote "${DB_INCIDENT_PDB_NAME:-}")"
  q_pdb_service="$(shell_quote "${DB_INCIDENT_PDB_SERVICE:-}")"
  q_sample_schemas="$(shell_quote "${DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED:-false}")"
  q_sysdba="$(shell_quote "${DB_INCIDENT_SYSDBA_CONNECT:-}")"
  q_datasafe_audit_enabled="$(shell_quote "${DB_INCIDENT_DATASAFE_AUDIT_ENABLED:-false}")"
  q_datasafe_failed_login="$(shell_quote "${DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED:-false}")"
  step "Running DB incident workload on demo jumphost"
  ssh_stdin <<EOF
set -euo pipefail
export DB_INCIDENT_ADMIN_CONNECT=$q_admin
export DB_INCIDENT_LAB_PASSWORD=$q_lab_password
export DB_INCIDENT_LAB_CONNECT=$q_lab_connect
export DB_INCIDENT_LAB_EZCONNECT=$q_lab_ezconnect
export DB_INCIDENT_PDB_NAME=$q_pdb_name
export DB_INCIDENT_PDB_SERVICE=$q_pdb_service
export DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=$q_sample_schemas
export DB_INCIDENT_SYSDBA_CONNECT=$q_sysdba
export DB_INCIDENT_DATASAFE_AUDIT_ENABLED=$q_datasafe_audit_enabled
export DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED=$q_datasafe_failed_login
cd $(shell_quote "$remote_packet_dir")
if [ "${DEMO_JUMPHOST_RUN_AS_ORACLE:-false}" = "true" ]; then
  sudo chown -R oracle "$remote_packet_dir"
  sudo --preserve-env=DB_INCIDENT_ADMIN_CONNECT,DB_INCIDENT_LAB_PASSWORD,DB_INCIDENT_LAB_CONNECT,DB_INCIDENT_LAB_EZCONNECT,DB_INCIDENT_PDB_NAME,DB_INCIDENT_PDB_SERVICE,DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED,DB_INCIDENT_SYSDBA_CONNECT,DB_INCIDENT_DATASAFE_AUDIT_ENABLED,DB_INCIDENT_DATASAFE_AUDIT_FAILED_LOGIN_ENABLED \\
    -u oracle -- bash -c 'exec ./run-db-incident-demo.sh'
else
  exec ./run-db-incident-demo.sh
fi
EOF
}

bastion_plan() {
  comp="$(compartment_id)"
  bastion_name="${DEMO_BASTION_NAME:-<DEMO_BASTION_NAME>}"
  step "OCI Bastion/jumphost execution plan"
  cat <<EOF
1. Resolve the OCI Bastion:
   oci --profile $PROFILE --region $REGION bastion bastion list \\
     --compartment-id <DEMO_DATABASE_COMPARTMENT_OCID> \\
     --query "data[?name=='$bastion_name'].id | [0]" --raw-output

2. Create a port-forwarding session to the DB host SSH port:
   oci --profile $PROFILE --region $REGION bastion session create-port-forwarding \\
     --bastion-id <BASTION_OCID> \\
     --ssh-public-key-file "\$DEMO_DB_SSH_KEY.pub" \\
     --target-private-ip "\$DEMO_DB_PRIVATE_IP" \\
     --target-port 22 \\
     --session-ttl 10800 \\
     --wait-for-state SUCCEEDED

3. Start the SSH tunnel:
   ssh -i "\$DEMO_DB_SSH_KEY" -N -L 8022:\$DEMO_DB_PRIVATE_IP:22 \\
     <SESSION_OCID>@host.bastion.$REGION.oci.oraclecloud.com

4. Copy and run the packet on the DB host:
   scp -i "\$DEMO_DB_SSH_KEY" -P 8022 ${OUTPUT_DIR%/}.tgz opc@127.0.0.1:/tmp/
   ssh -i "\$DEMO_DB_SSH_KEY" -p 8022 opc@127.0.0.1
   sudo su - oracle
   cd /tmp
   tar -xzf $(basename "${OUTPUT_DIR%/}.tgz")
   cd $(basename "$OUTPUT_DIR")
   export DB_INCIDENT_ADMIN_CONNECT='<local sysdba/admin connect string>'
   export DB_INCIDENT_LAB_PASSWORD='<disposable password>'
   export DB_INCIDENT_SAMPLE_SCHEMAS_ENABLED=true
   ./run-db-incident-demo.sh

5. Verify real DB evidence and wait for Management Agent ingestion:
   sqlplus -L -S /nolog @03-query-evidence.sql
   sqlplus -L -S /nolog @09-db-troubleshooting-queries.sql
   # Then query Log Analytics and run dbman-opsi db-incident from this repo.
EOF
  : "$comp"
}

logan_check() {
  if [ -z "$DATABASE_NAME" ]; then
    DATABASE_NAME="$(first_target_name)"
  fi
  require_value "$DATABASE_NAME" "DATABASE_NAME or DEMO_DATABASE_NAME"
  comp="$(compartment_id)"
  step "Live evidence query"
  PYTHONPATH=src "$PYTHON_BIN" -m dbman_opsi.cli db-incident \
    --profile "$PROFILE" \
    --region "$REGION" \
    --compartment-id "$comp" \
    --ora-code "${ORA_CODE:-ORA-00600}" \
    --database-name "$DATABASE_NAME" \
    --include-sources "${INCLUDE_SOURCES:-logan,dbm,opsi,datasafe}" \
    --hours-back "${HOURS_BACK:-24}" \
    --limit "${LIMIT:-20}" \
    --json
}

iso_window() {
  "$PYTHON_BIN" - "$1" <<'PY'
from datetime import UTC, datetime, timedelta
import sys
hours = int(sys.argv[1])
end = datetime.now(UTC).replace(microsecond=0)
start = end - timedelta(hours=hours)
print(start.isoformat().replace("+00:00", "Z"))
print(end.isoformat().replace("+00:00", "Z"))
PY
}

logan_scenario_check() {
  resolve_scenario_id
  comp="$(compartment_id)"
  ns="$(namespace)"
  lab_id="${LAB_ID:-lab-${SCENARIO_ID}}"
  query="'${SCENARIO_ID}' '${lab_id}' | sort -Time | head ${LIMIT:-50}"
  window="$(iso_window "${HOURS_BACK:-24}")"
  time_start="$(printf '%s\n' "$window" | sed -n '1p')"
  time_end="$(printf '%s\n' "$window" | sed -n '2p')"
  step "Log Analytics scenario query"
  info "scenario_id=$SCENARIO_ID lab_id=$lab_id"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics query search \
    --namespace-name "$ns" \
    --compartment-id "$comp" \
    --query-string "$query" \
    --sub-system LOG \
    --time-start "$time_start" \
    --time-end "$time_end" \
    --limit "${LIMIT:-50}" \
    --output json
}

wait_db() {
  require_oci_env
  [ -f "$CONFIG" ] || { fail "config missing: $CONFIG"; exit 2; }
  comp="$(compartment_id)"
  db_name="${DB_SYSTEM_NAME:-${DATABASE_NAME:-}}"
  require_value "$db_name" "DB_SYSTEM_NAME or DATABASE_NAME"
  interval="${DB_WAIT_SECONDS:-60}"
  max_minutes="${DB_WAIT_MAX_MINUTES:-90}"
  case "$interval:$max_minutes" in
    *[!0-9:]*|:*) fail "DB_WAIT_SECONDS and DB_WAIT_MAX_MINUTES must be whole numbers"; exit 2 ;;
  esac
  deadline=$(( $(date +%s) + max_minutes * 60 ))

  step "Waiting for disposable DBCS: $db_name"
  info "DBCS provisioning can take tens of minutes. This command only observes the lifecycle state."
  while :; do
    state="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" db system list \
      --compartment-id "$comp" --all \
      --query "data[?\"display-name\"==\`$db_name\`].\"lifecycle-state\" | [0]" --raw-output 2>/dev/null || true)"
    case "$state" in
      AVAILABLE)
        ok "DBCS is AVAILABLE"
        step "Next deployment steps"
        info "1. Import Terraform outputs: dbman-opsi import-tf-outputs --config $CONFIG"
        info "2. Generate dedicated-user and dashboard assets: dbman-opsi generate-disposable-assets --lifecycle-id <LIFECYCLE_ID>"
        info "3. Store role passwords in Vault, run the approved DB bootstrap path, then configure DBM/OPSI/Data Safe/Log Analytics."
        return 0
        ;;
      FAILED|TERMINATED)
        fail "DBCS entered terminal state: $state"
        info "Inspect OCI work requests and Terraform state before retrying."
        return 1
        ;;
      ""|null)
        warn "DBCS is not visible yet; retrying in ${interval}s"
        ;;
      *)
        info "DBCS state: $state; next check in ${interval}s"
        ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
      warn "Timed out after ${max_minutes} minutes; DBCS may still be provisioning. Re-run wait-db to continue monitoring."
      return 1
    fi
    sleep "$interval"
  done
}

tasks() {
  cat <<'TASKS'
DB Incident Demo E2E Task Checklist

Prerequisites:
  [ ] OCI profile can read demo database, DBM, OPSI, Log Analytics, Data Safe.
  [ ] Dedicated demo DB/PDB selected; do not run on existing production or shared PoC DBs.
  [ ] Demo jumphost/OCI Bastion path chosen and SSH key available.
  [ ] SQLplus or SQLcl available on the execution host.
  [ ] DB_INCIDENT_ADMIN_CONNECT points to the demo DB/PDB only.
  [ ] DB_INCIDENT_LAB_PASSWORD is set to a disposable password.
  [ ] Log Analytics namespace/log group exists.
  [ ] Generate Log Analytics packet: PYTHONPATH=src python -m dbman_opsi.cli generate-logan-payloads --config "$CONFIG" --output generated/logan-demo.
  [ ] Run generated/03-create-logan-management-agent-install-key.sh on the operator machine.
  [ ] Optionally run generated/11-resolve-logan-management-agent-package-url.sh to export AGENT_RPM_URL.
  [ ] Run generated/04-install-logan-management-agent.sh on the DB host or collector host, or use generated/07-bootstrap-logan-management-agent-ansible.sh + generated/08-run-logan-management-agent-ansible.sh from the operator machine.
  [ ] Run generated/05-verify-logan-management-agent.sh on the DB host or collector host.
  [ ] Run generated/06-resolve-logan-management-agent.sh on the operator machine and write the OCID into ignored config.
  [ ] Management Agent with Log Analytics plugin is installed on the DB host or collector host.
  [ ] Log Analytics entities exist for database and host.
  [ ] Database alert/audit/listener/trace sources are associated to the database entity.
  [ ] Linux/syslog/audit sources are associated to the host entity.
  [ ] Data Safe target is registered if security drilldown is part of the demo.
  [ ] Data Safe audit primer is enabled when you need real audit rows for export and correlation.
  [ ] Wait for DBCS to become AVAILABLE: scripts/demo-db-incident-e2e.sh wait-db.
  [ ] Import Terraform outputs into the ignored local config before service enablement.

Execution:
  [ ] Generate packet: scripts/demo-db-incident-e2e.sh generate.
  [ ] Package packet: scripts/demo-db-incident-e2e.sh package.
  [ ] Copy packet to demo jumphost or DB host.
  [ ] Run packet as the approved DB admin/oracle OS user.
  [ ] Confirm DBINC_LAB.incident_event_log has ORA/PLS events.
  [ ] If Data Safe correlation is in scope, set DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true before running the packet.
  [ ] Deliberate failed-login drills must use DBINC_LAB only. Do not test bad passwords against DBSNMP or other monitoring users.
  [ ] Confirm alert log contains optional synthetic markers only if SYSDBA marker script was run.
  [ ] Wait for Management Agent upload.
  [ ] Run scripts/demo-db-incident-e2e.sh logan-scenario-check until records appear.
  [ ] Query Log Analytics by scenario_id/lab_id and ORA codes.
  [ ] Run dbman-opsi db-incident evidence bundle.
  [ ] Ask LoganAI / oci-coordinator-oke /chat to correlate DB logs, DBM, OPSI, Audit, Data Safe, and missing source status.
  [ ] If the monitoring account is locked, run 12-check-monitoring-account-status.sql and 13-remediate-monitoring-account-lock.sql from the packet as DBA.
  [ ] Run cleanup after the demo.
TASKS
}

case "${1:-}" in
  prereq) check_prereq ;;
  generate) generate_packet ;;
  package) package_packet ;;
  jumphost-copy) jumphost_copy ;;
  jumphost-probe) jumphost_probe ;;
  jumphost-preflight) jumphost_preflight ;;
  jumphost-run) jumphost_run ;;
  logan-scenario-check) logan_scenario_check ;;
  wait-db) wait_db ;;
  bastion-plan) bastion_plan ;;
  logan-check) logan_check ;;
  tasks) tasks ;;
  -h|--help|help|"") usage ;;
  *) fail "unknown command: $1"; usage; exit 2 ;;
esac
