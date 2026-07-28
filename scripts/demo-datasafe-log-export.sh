#!/usr/bin/env bash
set -euo pipefail

CONFIG="${CONFIG:-dbman-opsi.local.yaml}"
PROFILE="${PROFILE:-${OCI_PROFILE:-}}"
REGION="${REGION:-${OCI_REGION:-}}"
OCI_BIN="${OCI_BIN:-oci}"
PYTHON_BIN="${PYTHON_BIN:-python}"
OUTPUT_DIR="${OUTPUT_DIR:-generated/datasafe-observability}"
HOURS_LOOKBACK="${HOURS_LOOKBACK:-24}"
DATASAFE_LOG_GROUP_NAME="${DATASAFE_LOG_GROUP_NAME:-dbman-opsi-datasafe-logs}"
DATASAFE_CUSTOM_LOG_NAME="${DATASAFE_CUSTOM_LOG_NAME:-dbman-opsi-datasafe-audit}"
DATASAFE_LA_LOG_GROUP_NAME="${DATASAFE_LA_LOG_GROUP_NAME:-dbman-opsi-datasafe-la}"
DATASAFE_TO_LOGAN_CONNECTOR_NAME="${DATASAFE_TO_LOGAN_CONNECTOR_NAME:-dbman-opsi-datasafe-to-logan}"
AUDIT_TO_LOGAN_CONNECTOR_NAME="${AUDIT_TO_LOGAN_CONNECTOR_NAME:-dbman-opsi-oci-audit-to-logan}"
POLICY_NAME="${POLICY_NAME:-dbman-opsi-datasafe-log-export}"
APPLY=false
ENSURED_DB_LOG_GROUP_OCID=""
ENSURED_DB_CUSTOM_LOG_OCID=""
ENSURED_LA_LOG_GROUP_OCID=""

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
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
fail() { printf '%sFAIL%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }
info() { printf '%sINFO%s %s\n' "$BLUE" "$RESET" "$1"; }

usage() {
  cat <<'EOF'
Usage: scripts/demo-datasafe-log-export.sh [--apply] <command>

Commands:
  prereq      Check OCI/Data Safe/Logging/Log Analytics prerequisites.
  plan        Print the exact demo-only Data Safe -> OCI Logging -> Log Analytics workflow.
  apply       Create or reuse OCI Logging objects, Log Analytics log group, and service connectors.
  sync        Seed/sync recent Data Safe audit events into the OCI Logging custom log.
  targets     Show Data Safe target/private-endpoint inventory for the configured compartment.
  status      Show Data Safe, OCI Logging, Service Connector, and Log Analytics status.
  dashboard   Write sanitized Log Analytics dashboard/query assets under generated/.

Notes:
  - Demo only. Do not use this workflow as-is for production.
  - Mutating commands require --apply.
  - Tenant-specific identifiers must come from env vars or the ignored local config.

Environment:
  CONFIG / PROFILE / REGION               Same meaning as the other demo scripts.
  HOURS_LOOKBACK                          Default 24; used by sync.
  DATASAFE_LOG_GROUP_NAME                 OCI Logging log group display name.
  DATASAFE_CUSTOM_LOG_NAME                OCI Logging custom log display name.
  DATASAFE_LA_LOG_GROUP_NAME              Log Analytics log group display name.
  DATASAFE_TO_LOGAN_CONNECTOR_NAME        Service Connector name for custom log -> Log Analytics.
  AUDIT_TO_LOGAN_CONNECTOR_NAME           Service Connector name for OCI Audit -> Log Analytics.
EOF
}

require_value() {
  value="$1"
  label="$2"
  [ -n "$value" ] || fail "$label is required"
}

config_value() {
  "$PYTHON_BIN" - "$CONFIG" "$1" <<'PY'
import sys, yaml
path, key = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    data = yaml.safe_load(handle) or {}
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

resolve_context() {
  [ -f "$CONFIG" ] || fail "config missing: $CONFIG"
  if [ -z "$PROFILE" ]; then
    PROFILE="$(config_value profile)"
  fi
  if [ -z "$REGION" ]; then
    REGION="$(config_value region)"
  fi
  COMPARTMENT_ID="${COMPARTMENT_ID:-$(config_value compartment_id)}"
  TENANCY_ID="${TENANCY_ID:-$(config_value tenancy_id)}"
  LA_NAMESPACE="${LA_NAMESPACE:-$(config_value log_analytics.namespace)}"
  require_value "$PROFILE" "PROFILE or OCI_PROFILE"
  require_value "$REGION" "REGION or OCI_REGION"
  require_value "$COMPARTMENT_ID" "compartment_id"
  require_value "$TENANCY_ID" "tenancy_id"
  if [ -z "$LA_NAMESPACE" ]; then
    LA_NAMESPACE="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" os ns get --query data --raw-output)"
  fi
}

require_apply() {
  [ "$APPLY" = true ] || fail "This command is mutating. Re-run with --apply."
}

log_group_id() {
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" logging log-group list \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$DATASAFE_LOG_GROUP_NAME" \
    --query 'data[0].id' --raw-output 2>/dev/null || true
}

custom_log_id() {
  group_id="$1"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" logging log list \
    --log-group-id "$group_id" \
    --display-name "$DATASAFE_CUSTOM_LOG_NAME" \
    --query 'data[0].id' --raw-output 2>/dev/null || true
}

la_log_group_id() {
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics log-group list \
    --namespace-name "$LA_NAMESPACE" \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$DATASAFE_LA_LOG_GROUP_NAME" \
    --query 'data.items[0].id' --raw-output 2>/dev/null || true
}

ensure_policy() {
  existing="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" iam policy list \
    --compartment-id "$COMPARTMENT_ID" \
    --name "$POLICY_NAME" \
    --query 'data[0].id' --raw-output 2>/dev/null || true)"
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    ok "IAM policy already exists"
    return 0
  fi
  policy_file="$(mktemp)"
  cat >"$policy_file" <<EOF
[
  "Allow any-user to use log-content in compartment id $COMPARTMENT_ID where all {request.principal.type='serviceconnector'}",
  "Allow any-user to use log-group in compartment id $COMPARTMENT_ID where all {request.principal.type='serviceconnector'}",
  "Allow any-user to {LOG_ANALYTICS_LOG_GROUP_UPLOAD_LOGS} in compartment id $COMPARTMENT_ID where all {request.principal.type='serviceconnector'}"
]
EOF
  if "$OCI_BIN" --profile "$PROFILE" --region "$REGION" iam policy create \
    --compartment-id "$COMPARTMENT_ID" \
    --name "$POLICY_NAME" \
    --description "Demo-only Data Safe audit export to OCI Logging and Log Analytics" \
    --statements "file://$policy_file" >/dev/null 2>&1; then
    ok "Created IAM policy for service connectors"
  else
    warn "IAM policy create failed. If connectors fail, create the service-connector policy statements manually."
  fi
  rm -f "$policy_file"
}

ensure_logging_resources() {
  group_id="$(log_group_id)"
  if [ -z "$group_id" ] || [ "$group_id" = "null" ]; then
    info "Creating OCI Logging log group: $DATASAFE_LOG_GROUP_NAME"
    "$OCI_BIN" --profile "$PROFILE" --region "$REGION" logging log-group create \
      --compartment-id "$COMPARTMENT_ID" \
      --display-name "$DATASAFE_LOG_GROUP_NAME" \
      --description "Demo-only Data Safe audit export" \
      >/dev/null
    sleep 3
    group_id="$(log_group_id)"
  else
    ok "Reusing OCI Logging log group"
  fi
  [ -n "$group_id" ] && [ "$group_id" != "null" ] || fail "Could not resolve OCI Logging log group OCID"

  log_id="$(custom_log_id "$group_id")"
  if [ -z "$log_id" ] || [ "$log_id" = "null" ]; then
    info "Creating OCI Logging custom log: $DATASAFE_CUSTOM_LOG_NAME"
    "$OCI_BIN" --profile "$PROFILE" --region "$REGION" logging log create \
      --log-group-id "$group_id" \
      --display-name "$DATASAFE_CUSTOM_LOG_NAME" \
      --log-type CUSTOM \
      --is-enabled true \
      --retention-duration 30 \
      >/dev/null
    sleep 3
    log_id="$(custom_log_id "$group_id")"
  else
    ok "Reusing OCI Logging custom log"
  fi
  [ -n "$log_id" ] && [ "$log_id" != "null" ] || fail "Could not resolve OCI Logging custom log OCID"

  la_group_id_value="$(la_log_group_id)"
  if [ -z "$la_group_id_value" ] || [ "$la_group_id_value" = "null" ]; then
    info "Creating Log Analytics log group: $DATASAFE_LA_LOG_GROUP_NAME"
    la_group_id_value="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics log-group create \
      --namespace-name "$LA_NAMESPACE" \
      --compartment-id "$COMPARTMENT_ID" \
      --display-name "$DATASAFE_LA_LOG_GROUP_NAME" \
      --description "Demo-only Data Safe audit views" \
      --query 'data.id' --raw-output)"
  else
    ok "Reusing Log Analytics log group"
  fi
  [ -n "$la_group_id_value" ] && [ "$la_group_id_value" != "null" ] || fail "Could not resolve Log Analytics log group OCID"

  ENSURED_DB_LOG_GROUP_OCID="$group_id"
  ENSURED_DB_CUSTOM_LOG_OCID="$log_id"
  ENSURED_LA_LOG_GROUP_OCID="$la_group_id_value"
}

connector_id() {
  display_name="$1"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" sch service-connector list \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$display_name" \
    --all \
    --query 'data.items[0].id' --raw-output 2>/dev/null || true
}

connector_state() {
  display_name="$1"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" sch service-connector list \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$display_name" \
    --all \
    --query 'data.items[0]."lifecycle-state"' --raw-output 2>/dev/null || true
}

wait_for_connector() {
  display_name="$1"
  attempts="${2:-30}"
  interval="${3:-10}"
  i=1
  while [ "$i" -le "$attempts" ]; do
    state="$(connector_state "$display_name")"
    case "$state" in
      ACTIVE)
        ok "Service Connector is ACTIVE: $display_name"
        return 0
        ;;
      FAILED)
        fail "Service Connector entered FAILED state: $display_name"
        ;;
      ""|null)
        :
        ;;
      *)
        info "Waiting for Service Connector $display_name (state=$state, attempt $i/$attempts)"
        ;;
    esac
    sleep "$interval"
    i=$((i + 1))
  done
  warn "Timed out waiting for Service Connector to become ACTIVE: $display_name"
  return 1
}

ensure_connector() {
  display_name="$1"
  source_json="$2"
  target_json="$3"
  existing="$(connector_id "$display_name")"
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    ok "Reusing Service Connector: $display_name"
    wait_for_connector "$display_name" 6 5 || true
    return 0
  fi
  info "Creating Service Connector: $display_name"
  if ! "$OCI_BIN" --profile "$PROFILE" --region "$REGION" sch service-connector create \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$display_name" \
    --source "$source_json" \
    --target "$target_json" >/dev/null; then
    warn "Service Connector create failed for $display_name. Check compartment limits or reuse an existing connector."
    return 1
  fi
  ok "Created Service Connector: $display_name"
  wait_for_connector "$display_name" 30 10 || true
}

search_logan_count() {
  query="$1"
  time_start="$2"
  time_end="$3"
  result="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics query search \
    --namespace-name "$LA_NAMESPACE" \
    --compartment-id "$COMPARTMENT_ID" \
    --query-string "$query" \
    --sub-system LOG \
    --time-start "$time_start" \
    --time-end "$time_end" \
    --limit 200 \
    --output json 2>/dev/null || true)"
  "$PYTHON_BIN" - "$result" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    print(0)
    raise SystemExit
rows = data.get("data", {}).get("items") or data.get("data", {}).get("results") or data.get("items") or data.get("results") or []
if isinstance(rows, dict):
    rows = rows.get("items") or []
print(len(rows) if isinstance(rows, list) else 0)
PY
}

write_dashboard_assets() {
  mkdir -p "$OUTPUT_DIR"
  cat >"$OUTPUT_DIR/datasafe-log-analytics-queries.md" <<'EOF'
# Data Safe Log Analytics Queries

These are demo-only query starters for the Data Safe -> OCI Logging -> Log Analytics bridge.

## Audit events by target and outcome
```text
'Log Source' = 'dbman-opsi-datasafe-audit'
| fields targetName, dbUserName, eventName, operationStatus, clientHostname, clientIp, objectName, objectType
| stats count as event_count by targetName, operationStatus, eventName
| sort -event_count
```

## Suspicious login / privilege activity
```text
'Log Source' = 'dbman-opsi-datasafe-audit'
| where operationStatus != 'SUCCESS' or like(lower(eventName), '%grant%') or like(lower(eventName), '%alter user%')
| fields auditEventTime, targetName, dbUserName, eventName, operationStatus, clientHostname, clientIp, sqlText
| sort -auditEventTime
```

## Correlate Data Safe audit with DB incident scenario markers
```text
('Log Source' = 'dbman-opsi-datasafe-audit') or like('Message', '%dbinc-%')
| fields datetime, Entity, 'Log Source', targetName, dbUserName, eventName, operationStatus, Message
| sort datetime desc
```
EOF

  cat >"$OUTPUT_DIR/datasafe-logan-dashboard.json" <<'EOF'
{
  "_description": "Demo-only Data Safe audit dashboard for OCI Log Analytics correlation.",
  "_source": "oci-dbman-opsi",
  "_version": 1,
  "dashboards": [
    {
      "name": "Data Safe Audit Overview",
      "widgets": [
        {
          "title": "Audit Events by Target",
          "visualization": "table",
          "query": "'Log Source' = 'dbman-opsi-datasafe-audit' | stats count as event_count by targetName, operationStatus, eventName | sort -event_count"
        },
        {
          "title": "Recent Failed or Sensitive Actions",
          "visualization": "table",
          "query": "'Log Source' = 'dbman-opsi-datasafe-audit' | where operationStatus != 'SUCCESS' or like(lower(eventName), '%grant%') or like(lower(eventName), '%alter user%') | fields auditEventTime, targetName, dbUserName, eventName, operationStatus, clientHostname, clientIp | sort auditEventTime desc"
        },
        {
          "title": "Cross-Source Correlation Starter",
          "visualization": "markdown",
          "content": "Use this dashboard with the DB incident dashboard and ask the agent to correlate Data Safe audit events with DB alert logs, host logs, OCI Audit, DBM, OPSI, and network context in the same time window."
        },
        {
          "title": "Runbook And Agent Drilldowns",
          "visualization": "markdown",
          "content": "Runbook: docs/datasafe-log-analytics.md\\nAgent prompt: Correlate Data Safe audit, DB alert logs, DBM, OPSI, OCI Audit, and VCN activity for the same incident window.\\nCLI: scripts/demo-datasafe-log-export.sh status"
        }
      ]
    }
  ]
}
EOF
  ok "Wrote dashboard/query assets to $OUTPUT_DIR"
}

start_time_rfc3339() {
  "$PYTHON_BIN" - "$HOURS_LOOKBACK" <<'PY'
from datetime import datetime, timedelta, timezone
import sys
hours = int(sys.argv[1])
value = datetime.now(timezone.utc) - timedelta(hours=hours)
print(value.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
PY
}

sync_audit_events() {
  group_id="$(log_group_id)"
  [ -n "$group_id" ] || fail "OCI Logging log group not found. Run apply first."
  log_id="$(custom_log_id "$group_id")"
  [ -n "$log_id" ] || fail "OCI Logging custom log not found. Run apply first."
  tmp_events="$(mktemp)"
  tmp_payload="$(mktemp)"
  start_time="$(start_time_rfc3339)"
  info "Fetching Data Safe audit events since $start_time"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe audit-event-summary list-audit-events \
    --compartment-id "$COMPARTMENT_ID" \
    --scim-query "(auditEventTime ge \"$start_time\")" \
    --all \
    --output json >"$tmp_events"

  "$PYTHON_BIN" - "$tmp_events" "$tmp_payload" <<'PY'
import json
import sys
from datetime import datetime, timezone

raw_path, payload_path = sys.argv[1:3]
with open(raw_path, encoding="utf-8") as handle:
    data = json.load(handle)

items = data.get("data", {})
if isinstance(items, dict):
    items = items.get("items", [])
if not isinstance(items, list):
    items = []

entries = []
for item in items[:500]:
    entries.append({
        "id": item.get("id", ""),
        "time": item.get("audit-event-time") or item.get("time-collected") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "data": json.dumps({
            "targetName": item.get("target-name", ""),
            "dbUserName": item.get("db-user-name", ""),
            "clientHostname": item.get("client-hostname", ""),
            "clientIp": item.get("client-ip", ""),
            "eventName": item.get("event-name", item.get("operation", "")),
            "operationStatus": item.get("operation-status", ""),
            "auditEventTime": item.get("audit-event-time", ""),
            "objectName": item.get("object-name", ""),
            "objectType": item.get("object-type", ""),
            "trailSource": item.get("trail-source", ""),
            "auditType": item.get("audit-type", ""),
            "sqlText": (item.get("sql-text") or "")[:500],
            "synthetic": False
        }),
    })

payload = {
    "specversion": "1.0",
    "logEntryBatches": [{
        "defaultlogentrytime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "source": "dbman-opsi-datasafe",
        "type": "com.oracle.datasafe.audit",
        "entries": entries,
    }],
}

with open(payload_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY

  entry_count="$("$PYTHON_BIN" - "$tmp_payload" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
batches = payload.get("logEntryBatches", [])
entries = batches[0].get("entries", []) if batches else []
print(len(entries))
PY
)"
  if [ "$entry_count" -eq 0 ]; then
    warn "No Data Safe audit events found in the lookback window"
  else
    "$OCI_BIN" --profile "$PROFILE" --region "$REGION" logging put-log-events \
      --log-group-id "$group_id" \
      --log-id "$log_id" \
      --put-logs-details "file://$tmp_payload" >/dev/null
    ok "Pushed $entry_count Data Safe audit events into OCI Logging"
  fi
  rm -f "$tmp_events" "$tmp_payload"
}

iso_window() {
  "$PYTHON_BIN" - "$HOURS_LOOKBACK" <<'PY'
from datetime import UTC, datetime, timedelta
import sys
hours = int(sys.argv[1])
end = datetime.now(UTC).replace(microsecond=0)
start = end - timedelta(hours=hours)
print(start.isoformat().replace("+00:00", "Z"))
print(end.isoformat().replace("+00:00", "Z"))
PY
}

recent_datasafe_audit_count() {
  start_time="$(start_time_rfc3339)"
  tmp_events="$(mktemp)"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe audit-event-summary list-audit-events \
    --compartment-id "$COMPARTMENT_ID" \
    --scim-query "(auditEventTime ge \"$start_time\")" \
    --all \
    --output json >"$tmp_events"
  "$PYTHON_BIN" - "$tmp_events" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
items = data.get("data", {})
if isinstance(items, dict):
    items = items.get("items", [])
print(len(items) if isinstance(items, list) else 0)
PY
  rm -f "$tmp_events"
}

audit_profile_count() {
  tmp_profiles="$(mktemp)"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe audit-profile list \
    --compartment-id "$COMPARTMENT_ID" \
    --all \
    --output json >"$tmp_profiles" 2>/dev/null || {
      rm -f "$tmp_profiles"
      printf '0\n'
      return 0
    }
  "$PYTHON_BIN" - "$tmp_profiles" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
items = data.get("data") or []
print(len(items) if isinstance(items, list) else 0)
PY
  rm -f "$tmp_profiles"
}

audit_trail_count() {
  tmp_trails="$(mktemp)"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe audit-trail list \
    --compartment-id "$COMPARTMENT_ID" \
    --all \
    --output json >"$tmp_trails" 2>/dev/null || {
      rm -f "$tmp_trails"
      printf '0\n'
      return 0
    }
  "$PYTHON_BIN" - "$tmp_trails" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
items = data.get("data") or []
print(len(items) if isinstance(items, list) else 0)
PY
  rm -f "$tmp_trails"
}

cmd_prereq() {
  resolve_context
  step "Local tooling"
  command -v "$OCI_BIN" >/dev/null 2>&1 && ok "OCI CLI found" || fail "OCI CLI not found"
  command -v "$PYTHON_BIN" >/dev/null 2>&1 && ok "Python found" || fail "Python not found"

  step "OCI services"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe target-database list --compartment-id "$COMPARTMENT_ID" >/dev/null
  ok "Data Safe target list works"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" logging log-group list --compartment-id "$COMPARTMENT_ID" >/dev/null
  ok "OCI Logging list works"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" log-analytics log-group list --namespace-name "$LA_NAMESPACE" --compartment-id "$COMPARTMENT_ID" >/dev/null
  ok "Log Analytics log group list works"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" sch service-connector list --compartment-id "$COMPARTMENT_ID" >/dev/null
  ok "Service Connector list works"
}

cmd_targets() {
  resolve_context
  step "Data Safe target databases"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe target-database list \
    --compartment-id "$COMPARTMENT_ID" \
    --all \
    --query 'data[].{name:"display-name",state:"lifecycle-state",type:"database-type"}' \
    --output table
  step "Data Safe private endpoints"
  "$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe private-endpoint list \
    --compartment-id "$COMPARTMENT_ID" \
    --all \
    --query 'data[].{name:"display-name",state:"lifecycle-state"}' \
    --output table
}

cmd_status() {
  resolve_context
  step "Data Safe export bridge status"
  group_id="$(log_group_id)"
  log_id=""
  la_group_id_value="$(la_log_group_id)"
  if [ -n "$group_id" ] && [ "$group_id" != "null" ]; then
    log_id="$(custom_log_id "$group_id")"
    ok "OCI Logging log group present"
  else
    warn "OCI Logging log group is not present"
  fi
  if [ -n "$log_id" ] && [ "$log_id" != "null" ]; then
    ok "OCI Logging custom log present"
  else
    warn "OCI Logging custom log is not present"
  fi
  if [ -n "$la_group_id_value" ] && [ "$la_group_id_value" != "null" ]; then
    ok "Log Analytics log group present"
  else
    warn "Log Analytics log group is not present"
  fi
  ds_connector_state="$(connector_state "$DATASAFE_TO_LOGAN_CONNECTOR_NAME")"
  audit_connector_state="$(connector_state "$AUDIT_TO_LOGAN_CONNECTOR_NAME")"
  info "Service Connector $DATASAFE_TO_LOGAN_CONNECTOR_NAME state=${ds_connector_state:-missing}"
  info "Service Connector $AUDIT_TO_LOGAN_CONNECTOR_NAME state=${audit_connector_state:-missing}"

  target_count="$("$OCI_BIN" --profile "$PROFILE" --region "$REGION" data-safe target-database list --compartment-id "$COMPARTMENT_ID" --all --query 'length(data)' --raw-output 2>/dev/null || printf '0')"
  info "Data Safe target databases in compartment: $target_count"
  profile_count="$(audit_profile_count)"
  trail_count="$(audit_trail_count)"
  info "Data Safe audit profiles in compartment: $profile_count"
  info "Data Safe audit trails in compartment: $trail_count"
  if [ "$target_count" -gt 0 ] && [ "$profile_count" -eq 0 ]; then
    warn "Targets are registered but no Data Safe audit profiles exist. Audit collection has not been provisioned yet for this compartment."
  fi
  if [ "$profile_count" -gt 0 ] && [ "$trail_count" -eq 0 ]; then
    warn "Data Safe audit profiles exist but no audit trails are discovered or started yet."
  fi
  audit_count="$(recent_datasafe_audit_count)"
  info "Recent Data Safe audit events in last ${HOURS_LOOKBACK}h: $audit_count"
  if [ "$audit_count" -eq 0 ]; then
    warn "No recent Data Safe audit rows found. Run the DB incident packet with DB_INCIDENT_DATASAFE_AUDIT_ENABLED=true using DBINC_LAB activity only, then re-run sync/status."
  fi

  window="$(iso_window)"
  time_start="$(printf '%s\n' "$window" | sed -n '1p')"
  time_end="$(printf '%s\n' "$window" | sed -n '2p')"
  if [ -n "$la_group_id_value" ] && [ "$la_group_id_value" != "null" ]; then
    hits="$(search_logan_count "'Log Source' = 'dbman-opsi-datasafe-audit' | sort -Time | head 50" "$time_start" "$time_end")"
    info "Recent Log Analytics rows for dbman-opsi-datasafe-audit in last ${HOURS_LOOKBACK}h: $hits"
    if [ "$audit_count" -gt 0 ] && [ "$hits" -eq 0 ]; then
      warn "Data Safe rows exist but are not yet searchable in Log Analytics. Re-run sync/status after connector and ingestion delay."
    fi
  fi
}

cmd_plan() {
  resolve_context
  cat <<EOF
Demo-only Data Safe export workflow

1. Reuse or create:
   - OCI Logging log group: $DATASAFE_LOG_GROUP_NAME
   - OCI Logging custom log: $DATASAFE_CUSTOM_LOG_NAME
   - Log Analytics log group: $DATASAFE_LA_LOG_GROUP_NAME
2. Reuse or create Service Connectors:
   - $DATASAFE_TO_LOGAN_CONNECTOR_NAME   (custom log -> Log Analytics)
   - $AUDIT_TO_LOGAN_CONNECTOR_NAME      (OCI Audit -> Log Analytics)
3. Confirm Data Safe target databases and private endpoints are present.
4. Seed and sync recent Data Safe audit events into the custom log.
5. Validate recent rows and import dashboard/query assets from:
   $OUTPUT_DIR

This workflow is for showcasing OCI Observability capabilities only.
Do not use it unchanged for production.
EOF
}

cmd_apply() {
  resolve_context
  require_apply
  ensure_policy
  ensure_logging_resources
  printf 'C15_DB_LOG_GROUP_OCID=%s\n' "$ENSURED_DB_LOG_GROUP_OCID"
  printf 'C15_DB_CUSTOM_LOG_OCID=%s\n' "$ENSURED_DB_CUSTOM_LOG_OCID"
  printf 'C15_LA_LOG_GROUP_OCID=%s\n' "$ENSURED_LA_LOG_GROUP_OCID"
  source_json="{\"kind\":\"logging\",\"logSources\":[{\"compartmentId\":\"$COMPARTMENT_ID\",\"logGroupId\":\"$ENSURED_DB_LOG_GROUP_OCID\",\"logId\":\"$ENSURED_DB_CUSTOM_LOG_OCID\"}]}"
  target_json="{\"kind\":\"loggingAnalytics\",\"logGroupId\":\"$ENSURED_LA_LOG_GROUP_OCID\"}"
  ensure_connector "$DATASAFE_TO_LOGAN_CONNECTOR_NAME" "$source_json" "$target_json"
  audit_source_json="{\"kind\":\"logging\",\"logSources\":[{\"compartmentId\":\"$COMPARTMENT_ID\",\"logGroupId\":\"_Audit\"}]}"
  ensure_connector "$AUDIT_TO_LOGAN_CONNECTOR_NAME" "$audit_source_json" "$target_json"
  write_dashboard_assets
}

cmd_sync() {
  resolve_context
  require_apply
  sync_audit_events
}

cmd_dashboard() {
  write_dashboard_assets
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done

command_name="${1:-}"
case "$command_name" in
  prereq) cmd_prereq ;;
  plan) cmd_plan ;;
  apply) cmd_apply ;;
  sync) cmd_sync ;;
  targets) cmd_targets ;;
  status) cmd_status ;;
  dashboard) cmd_dashboard ;;
  "") usage; exit 0 ;;
  *) usage; fail "unknown command: $command_name" ;;
esac
