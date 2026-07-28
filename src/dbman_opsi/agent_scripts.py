"""Management Agent script generation for external targets and Log Analytics collectors."""

from __future__ import annotations

import re
from pathlib import Path

from dbman_opsi.config import EnablementConfig, Target


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "target"


def _operator_script_name(target: Target, suffix: str) -> str:
    return f"{_slug(target.name)}-{suffix}.sh"


def _host_script_name(target: Target, suffix: str) -> str:
    return f"{_slug(target.name)}-{suffix}.sh"


def _agent_display_name(target: Target) -> str:
    return target.logan_hostname or target.external_host or target.name


def _needs_management_agent(target: Target) -> bool:
    if target.kind in {"external-db", "external-exadata"}:
        return True
    return target.wants("logan") and target.kind in {"dbcs", "exadata"}


def _plugin_download_lines(target: Target) -> list[str]:
    lines: list[str] = []
    if target.kind in {"external-db", "external-exadata"}:
        if target.wants("dbm"):
            lines.append("Service.plugin.dbmgmt.download=true")
        if target.wants("opsi"):
            lines.append("Service.plugin.opsi.download=true")
    if target.wants("logan"):
        lines.append("Service.plugin.logan.download=true")
    return lines


def _shell_ui() -> str:
    return """if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET="$(printf '\\033[0m')"
  C_RED="$(printf '\\033[31m')"
  C_GREEN="$(printf '\\033[32m')"
  C_YELLOW="$(printf '\\033[33m')"
  C_BLUE="$(printf '\\033[34m')"
else
  C_RESET=""
  C_RED=""
  C_GREEN=""
  C_YELLOW=""
  C_BLUE=""
fi

fail() { printf '%sFAIL%s %s\\n' "$C_RED" "$C_RESET" "$1" >&2; exit 1; }
info() { printf '%sINFO%s %s\\n' "$C_BLUE" "$C_RESET" "$1"; }
warn() { printf '%sWARN%s %s\\n' "$C_YELLOW" "$C_RESET" "$1"; }
ok() { printf '%sOK%s %s\\n' "$C_GREEN" "$C_RESET" "$1"; }
"""


def _linux_install_script(target: Target, config: EnablementConfig) -> str:
    plugin_lines = "\n".join(_plugin_download_lines(target))
    display_name = _agent_display_name(target)
    target_label = target.name
    return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

AGENT_RPM="${{AGENT_RPM:-}}"
AGENT_RPM_URL="${{AGENT_RPM_URL:-}}"
AGENT_RPM_SHA256="${{AGENT_RPM_SHA256:-}}"
INSTALL_KEY="${{INSTALL_KEY:-}}"
INSTALL_KEY_FILE="${{INSTALL_KEY_FILE:-}}"
DELETE_INSTALL_KEY_FILE="${{DELETE_INSTALL_KEY_FILE:-false}}"
RSP_FILE="${{RSP_FILE:-}}"
AGENT_HOME="${{AGENT_HOME:-/opt/oracle/mgmt_agent/agent_inst}}"
WORK_RPM="${{WORK_RPM:-/tmp/dbman-opsi-mgmt-agent.rpm}}"
JAVA8_HOME="${{JAVA8_HOME:-}}"

{_shell_ui()}

if [ -z "$RSP_FILE" ]; then
  RSP_FILE="$(mktemp "${{TMPDIR:-/tmp}}/dbman-opsi-agent.XXXXXX.rsp")"
fi
cleanup() {{
  rm -f -- "$RSP_FILE"
  if [ "$DELETE_INSTALL_KEY_FILE" = "true" ] && [ -n "$INSTALL_KEY_FILE" ]; then
    rm -f -- "$INSTALL_KEY_FILE"
  fi
}}
trap cleanup EXIT

load_install_key() {{
  if [ -n "$INSTALL_KEY" ]; then
    printf '%s' "$INSTALL_KEY"
    return 0
  fi
  if [ -n "$INSTALL_KEY_FILE" ] && [ -f "$INSTALL_KEY_FILE" ]; then
    sed -n 's/^[[:space:]]*ManagementAgentInstallKey[[:space:]]*=[[:space:]]*//p' "$INSTALL_KEY_FILE" | head -1
    return 0
  fi
  return 1
}}

install_pkg() {{
  pkg="$1"
  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y "$pkg"
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y "$pkg"
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
  else
    fail "No supported package manager found to install $pkg"
  fi
}}

java8_version_ok() {{
  java_bin="$1"
  [ -x "$java_bin" ] || return 1
  version_line="$("$java_bin" -version 2>&1 | head -1 || true)"
  case "$version_line" in
    *\"1.8.0_\"*)
      update_part="$(printf '%s' "$version_line" | sed -n 's/.*"1\\.8\\.0_\\([0-9][0-9]*\\).*/\\1/p')"
      [ -n "$update_part" ] || return 1
      [ "$update_part" -ge 281 ]
      ;;
    *)
      return 1
      ;;
  esac
}}

resolve_java8() {{
  if [ -n "$JAVA8_HOME" ] && java8_version_ok "$JAVA8_HOME/bin/java"; then
    printf '%s' "$JAVA8_HOME/bin/java"
    return 0
  fi
  if java8_version_ok /usr/bin/java; then
    printf '%s' /usr/bin/java
    return 0
  fi
  for candidate in \
    /usr/lib/jvm/java-1.8.0-openjdk*/jre/bin/java \
    /usr/lib/jvm/jre-1.8.0-openjdk/bin/java \
    /usr/java/latest/bin/java
  do
    for java_bin in $candidate; do
      if java8_version_ok "$java_bin"; then
        printf '%s' "$java_bin"
        return 0
      fi
    done
  done
  return 1
}}

ensure_java8() {{
  if java_bin="$(resolve_java8)"; then
    ok "Java 8 runtime found: $java_bin"
  else
    warn "Java 8u281+ was not found. Installing a compatible runtime for the demo agent path."
    if command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
      install_pkg java-1.8.0-openjdk-headless
    elif command -v apt-get >/dev/null 2>&1; then
      install_pkg openjdk-8-jre-headless
    else
      fail "Install Java 8u281+ and re-run the script"
    fi
    java_bin="$(resolve_java8 || true)"
    [ -n "$java_bin" ] || fail "Java 8u281+ is still unavailable after installation"
  fi

  JAVA_HOME="$(cd "$(dirname "$java_bin")/.." && pwd)"
  export JAVA_HOME
  if command -v alternatives >/dev/null 2>&1; then
    sudo alternatives --install /usr/bin/java java "$java_bin" 1800492 >/dev/null 2>&1 || true
    sudo alternatives --set java "$java_bin" >/dev/null 2>&1 || true
  fi
  info "Using JAVA_HOME=$JAVA_HOME"
}}

verify_agent_rpm() {{
  [ -n "$AGENT_RPM_SHA256" ] || fail "Set AGENT_RPM_SHA256 when AGENT_RPM_URL is used"
  if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256="$(sha256sum "$AGENT_RPM" | awk '{{print $1}}')"
  elif command -v shasum >/dev/null 2>&1; then
    actual_sha256="$(shasum -a 256 "$AGENT_RPM" | awk '{{print $1}}')"
  else
    fail "sha256sum or shasum is required to verify the Management Agent RPM"
  fi
  [ "$actual_sha256" = "$AGENT_RPM_SHA256" ] || fail "SHA256 mismatch for $AGENT_RPM"
  ok "Verified Management Agent RPM SHA256"
}}

if [ -z "$AGENT_RPM" ]; then
  [ -n "$AGENT_RPM_URL" ] || fail "Set AGENT_RPM or AGENT_RPM_URL"
  [ -n "$AGENT_RPM_SHA256" ] || fail "Set AGENT_RPM_SHA256 when AGENT_RPM_URL is used"
  case "$AGENT_RPM_URL" in
    https://*) ;;
    *) fail "AGENT_RPM_URL must use HTTPS" ;;
  esac
  command -v curl >/dev/null 2>&1 || fail "curl is required when AGENT_RPM_URL is used"
  info "Downloading OCI Management Agent RPM from AGENT_RPM_URL"
  curl --proto '=https' --tlsv1.2 -fsSL "$AGENT_RPM_URL" -o "$WORK_RPM"
  AGENT_RPM="$WORK_RPM"
  verify_agent_rpm
fi
[ -f "$AGENT_RPM" ] || fail "AGENT_RPM file not found: $AGENT_RPM"
if [ -n "$AGENT_RPM_SHA256" ] && [ "$AGENT_RPM" != "$WORK_RPM" ]; then
  verify_agent_rpm
fi
RESOLVED_INSTALL_KEY="$(load_install_key || true)"
[ -n "$RESOLVED_INSTALL_KEY" ] || fail "Set INSTALL_KEY or INSTALL_KEY_FILE"
ensure_java8

cat >"$RSP_FILE" <<EOF
ManagementAgentInstallKey=$RESOLVED_INSTALL_KEY
CredentialWalletPassword=
{plugin_lines}
Region={config.region}
CompartmentId={config.compartment_id or ""}
DisplayName={display_name}
EOF

if [ ! -x "$AGENT_HOME/bin/setup.sh" ]; then
  info "Installing OCI Management Agent package"
  sudo env JAVA_HOME="$JAVA_HOME" rpm -Uvh "$AGENT_RPM"
else
  warn "OCI Management Agent package already present; re-running setup to apply desired plugins"
fi

sudo env JAVA_HOME="$JAVA_HOME" "$AGENT_HOME/bin/setup.sh" opts="$RSP_FILE"
sudo systemctl enable mgmt_agent || true
sudo systemctl restart mgmt_agent || sudo "$AGENT_HOME/bin/agentctl" start
sleep 10
if [ -x "$AGENT_HOME/bin/agentctl" ]; then
  sudo "$AGENT_HOME/bin/agentctl" status || true
else
  warn "agentctl not present under $AGENT_HOME/bin; checking systemd and process state instead"
  sudo systemctl status mgmt_agent --no-pager || true
  sudo ps -ef | egrep 'mgmt_agent|oracle\\.polaris' | grep -v egrep || true
fi
ok "Management Agent install and setup completed"

cat <<EOF

Management Agent bootstrap finished for {target_label}.
Next steps:
1. Run the generated verify script on this host.
2. Run the generated resolve script from the operator machine to capture the agent OCID.
3. Re-run dbman-opsi log-analytics --apply after writing management_agent_id or logan_management_agent_id into the ignored config.
EOF
"""


def _windows_script(target: Target, config: EnablementConfig) -> str:
    plugin_lines = "\r\n".join(_plugin_download_lines(target))
    display_name = _agent_display_name(target)
    return f"""$ErrorActionPreference = "Stop"
$AgentZip = $env:AGENT_ZIP
$InstallKey = $env:INSTALL_KEY
$RspFile = "C:\\oci-mgmt-agent\\dbman-opsi-agent.rsp"
if (-not $AgentZip) {{ throw "Set AGENT_ZIP to the downloaded OCI Management Agent zip" }}
if (-not $InstallKey) {{ throw "Set INSTALL_KEY to the OCI Management Agent install key" }}

try {{
  Expand-Archive -Path $AgentZip -DestinationPath C:\\oci-mgmt-agent -Force
@"
ManagementAgentInstallKey=$InstallKey
CredentialWalletPassword=
{plugin_lines}
Region={config.region}
CompartmentId={config.compartment_id or ""}
DisplayName={display_name}
"@ | Out-File -Encoding ascii $RspFile

  & C:\\oci-mgmt-agent\\setup.bat "opts=$RspFile"
}} finally {{
  Remove-Item -Force -ErrorAction SilentlyContinue $RspFile
}}
"""


def _generic_unix_script(target: Target, config: EnablementConfig) -> str:
    plugins = ", ".join(line.split(".")[2] for line in _plugin_download_lines(target)) or "logan"
    return f"""#!/usr/bin/env sh
set -eu

echo "Install OCI Management Agent using the platform package for {target.external_os}."
echo "Use install key from OCI Management Agent service; do not write it into repo files."
echo "Required plugins: {plugins}"
echo "Region: {config.region}"
echo "Compartment: {config.compartment_id or ''}"
echo "Display name: {_agent_display_name(target)}"
"""


def render_agent_script(target: Target, config: EnablementConfig) -> str:
    if target.external_os == "windows":
        return _windows_script(target, config)
    if target.external_os == "linux" or target.external_os is None:
        return _linux_install_script(target, config)
    return _generic_unix_script(target, config)


def render_agent_verify_script(target: Target, config: EnablementConfig) -> str:
    _ = config
    plugin_patterns = " ".join(_plugin_download_lines(target)) or "Service.plugin.logan.download=true"
    return f"""#!/usr/bin/env bash
set -euo pipefail

AGENT_HOME="${{AGENT_HOME:-/opt/oracle/mgmt_agent/agent_inst}}"

{_shell_ui()}

[ -d "$AGENT_HOME" ] || fail "Management Agent home not found under $AGENT_HOME"

if [ -x "$AGENT_HOME/bin/agentctl" ]; then
  info "Management Agent status"
  sudo "$AGENT_HOME/bin/agentctl" status || true
else
  warn "agentctl not present under $AGENT_HOME/bin; skipping direct agentctl check"
fi

info "Service state"
sudo systemctl status mgmt_agent --no-pager || true

info "Process state"
sudo ps -ef | egrep 'mgmt_agent|oracle\\.polaris' | grep -v egrep || true

info "Recent Management Agent logs"
sudo find "$AGENT_HOME/log" -maxdepth 1 -type f 2>/dev/null | sort || true
sudo tail -20 "$AGENT_HOME/log/mgmt_agent.log" 2>/dev/null || true
sudo tail -20 "$AGENT_HOME/log/mgmt_agent_logan.log" 2>/dev/null || true

info "Plugin marker search"
sudo find "$AGENT_HOME" -maxdepth 5 \\( -iname '*logan*' -o -iname '*dbmgmt*' -o -iname '*opsi*' \\) 2>/dev/null || true

info "Expected plugin flags"
printf '%s\\n' {plugin_patterns}

info "Host identity"
hostname -f 2>/dev/null || hostname
ok "Verification finished"

cat <<EOF

After this check:
1. Run 00-discover-logan-host-facts.sh on the collector/DB host.
2. Run the generated resolve-agent script from the operator machine.
3. Write the returned Management Agent OCID into management_agent_id or logan_management_agent_id in the ignored config.
EOF
"""


def render_agent_install_key_script(target: Target, config: EnablementConfig) -> str:
    target_slug = _slug(target.name)
    return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

PROFILE="${{PROFILE:-{config.profile}}}"
REGION="${{REGION:-{config.region}}}"
COMPARTMENT_ID="${{COMPARTMENT_ID:-{config.compartment_id or ""}}}"
INSTALL_KEY_DISPLAY_NAME="${{INSTALL_KEY_DISPLAY_NAME:-dbman-opsi-{target_slug}-agent}}"
INSTALL_KEY_JSON="${{INSTALL_KEY_JSON:-./{target_slug}-mgmt-agent-install-key.json}}"
INSTALL_KEY_FILE="${{INSTALL_KEY_FILE:-./{target_slug}-mgmt-agent-install-key.rsp}}"
INSTALL_KEY_UNLIMITED="${{INSTALL_KEY_UNLIMITED:-true}}"
ALLOWED_INSTALL_COUNT="${{ALLOWED_INSTALL_COUNT:-3}}"
INSTALL_KEY_EXPIRES_AT="${{INSTALL_KEY_EXPIRES_AT:-}}"

{_shell_ui()}

command -v oci >/dev/null 2>&1 || fail "OCI CLI not found"
[ -n "$COMPARTMENT_ID" ] || fail "Set COMPARTMENT_ID"

extra_args=()
if [ "$INSTALL_KEY_UNLIMITED" = "true" ]; then
  extra_args+=(--is-unlimited true)
else
  [ -n "$INSTALL_KEY_EXPIRES_AT" ] || fail "Set INSTALL_KEY_EXPIRES_AT when INSTALL_KEY_UNLIMITED=false"
  extra_args+=(--is-unlimited false --time-expires "$INSTALL_KEY_EXPIRES_AT" --allowed-key-install-count "$ALLOWED_INSTALL_COUNT")
fi

oci --profile "$PROFILE" --region "$REGION" management-agent install-key create \\
  --compartment-id "$COMPARTMENT_ID" \\
  --display-name "$INSTALL_KEY_DISPLAY_NAME" \\
  "${{extra_args[@]}}" \\
  --wait-for-state ACTIVE \\
  --max-wait-seconds 300 \\
  --wait-interval-seconds 10 \\
  --output json >"$INSTALL_KEY_JSON"

INSTALL_KEY_ID="$(python - "$INSTALL_KEY_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["data"]["id"])
PY
)"

if ! oci --profile "$PROFILE" --region "$REGION" management-agent install-key get-install-key-content \\
  --install-key-id "$INSTALL_KEY_ID" \\
  --file "$INSTALL_KEY_FILE" 2>/dev/null; then
  warn "OCI CLI did not accept --install-key-id; retrying with --management-agent-install-key-id"
  oci --profile "$PROFILE" --region "$REGION" management-agent install-key get-install-key-content \\
    --management-agent-install-key-id "$INSTALL_KEY_ID" \\
    --file "$INSTALL_KEY_FILE"
fi
chmod 600 "$INSTALL_KEY_JSON" "$INSTALL_KEY_FILE"

ok "Wrote install key metadata to $INSTALL_KEY_JSON"
ok "Wrote install key content to $INSTALL_KEY_FILE"
info "Use INSTALL_KEY_FILE=$INSTALL_KEY_FILE with the generated host install script."
"""


def render_agent_package_url_script(target: Target, config: EnablementConfig) -> str:
    _ = target
    return f"""#!/usr/bin/env bash
set -euo pipefail

PROFILE="${{PROFILE:-{config.profile}}}"
REGION="${{REGION:-{config.region}}}"
COMPARTMENT_ID="${{COMPARTMENT_ID:-{config.compartment_id or ""}}}"
PACKAGE_JSON="${{PACKAGE_JSON:-./management-agent-image-linux-rpm.json}}"
OUTPUT_RPM="${{OUTPUT_RPM:-./oracle.mgmt_agent.rpm}}"
DOWNLOAD_AGENT="${{DOWNLOAD_AGENT:-false}}"

{_shell_ui()}

command -v oci >/dev/null 2>&1 || fail "OCI CLI not found"
[ -n "$COMPARTMENT_ID" ] || fail "Set COMPARTMENT_ID"

oci --profile "$PROFILE" --region "$REGION" management-agent agent-image list \\
  --compartment-id "$COMPARTMENT_ID" \\
  --all \\
  --output json >"$PACKAGE_JSON"

eval "$(
python - "$PACKAGE_JSON" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
items = payload.get("data", []) or []

def norm(value):
    return str(value or "").strip().upper()

matches = []
for item in items:
    if norm(item.get("lifecycle-state")) not in {{"", "ACTIVE"}}:
        continue
    if "LINUX" not in norm(item.get("platform-type")) and "LINUX" not in norm(item.get("platform-name")):
        continue
    if "RPM" != norm(item.get("package-type")):
        continue
    details = item.get("image-object-storage-details") or {{}}
    namespace = details.get("object-namespace") or ""
    bucket = details.get("object-bucket") or ""
    name = details.get("object-name") or ""
    checksum = details.get("checksum") or item.get("checksum") or ""
    url = item.get("object-url") or details.get("object-url") or ""
    if namespace and bucket and name:
        matches.append((str(item.get("time-created") or ""), namespace, bucket, name, checksum, url))

matches.sort(reverse=True)
if not matches:
    raise SystemExit("FAIL no active Linux RPM Management Agent image found")
_, namespace, bucket, name, checksum, url = matches[0]
for key, value in {{
    "AGENT_RPM_OBJECT_NAMESPACE": namespace,
    "AGENT_RPM_OBJECT_BUCKET": bucket,
    "AGENT_RPM_OBJECT_NAME": name,
    "AGENT_RPM_SHA256": checksum,
    "AGENT_RPM_URL": url,
}}.items():
    print(f"{{key}}='{{value}}'")
PY
)"

ok "Wrote agent-image payload to $PACKAGE_JSON"
printf 'AGENT_RPM_OBJECT_NAMESPACE=%s\\n' "$AGENT_RPM_OBJECT_NAMESPACE"
printf 'AGENT_RPM_OBJECT_BUCKET=%s\\n' "$AGENT_RPM_OBJECT_BUCKET"
printf 'AGENT_RPM_OBJECT_NAME=%s\\n' "$AGENT_RPM_OBJECT_NAME"
printf 'AGENT_RPM_SHA256=%s\\n' "$AGENT_RPM_SHA256"
printf 'AGENT_RPM_URL=%s\\n' "$AGENT_RPM_URL"

if [ "$DOWNLOAD_AGENT" = "true" ]; then
  info "Downloading the RPM with authenticated OCI Object Storage access"
  oci --profile "$PROFILE" --region "$REGION" os object get \\
    --namespace-name "$AGENT_RPM_OBJECT_NAMESPACE" \\
    --bucket-name "$AGENT_RPM_OBJECT_BUCKET" \\
    --name "$AGENT_RPM_OBJECT_NAME" \\
    --file "$OUTPUT_RPM" \\
    --no-multipart
  ok "Downloaded OCI Management Agent RPM to $OUTPUT_RPM"
  if command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$OUTPUT_RPM" | awk '{{print $1}}')"
    [ "$actual" = "$AGENT_RPM_SHA256" ] || fail "SHA256 mismatch for $OUTPUT_RPM"
  fi
  printf 'AGENT_RPM=%s\\n' "$OUTPUT_RPM"
else
  warn "The object URL may not be directly fetchable without OCI auth. Prefer DOWNLOAD_AGENT=true to materialize a local RPM."
fi
info "Use AGENT_RPM with the generated host install or Ansible run script."
"""


def render_agent_resolve_script(target: Target, config: EnablementConfig) -> str:
    host_hint = target.logan_hostname or target.external_host or ""
    display_name = _agent_display_name(target)
    return f"""#!/usr/bin/env bash
set -euo pipefail

PROFILE="${{PROFILE:-{config.profile}}}"
REGION="${{REGION:-{config.region}}}"
COMPARTMENT_ID="${{COMPARTMENT_ID:-{config.compartment_id or ""}}}"
AGENT_DISPLAY_NAME="${{AGENT_DISPLAY_NAME:-{display_name}}}"
AGENT_HOSTNAME_HINT="${{AGENT_HOSTNAME_HINT:-{host_hint}}}"

{_shell_ui()}

command -v oci >/dev/null 2>&1 || fail "OCI CLI not found"
[ -n "$COMPARTMENT_ID" ] || fail "Set COMPARTMENT_ID"

TMP_JSON="$(mktemp)"
trap 'rm -f "$TMP_JSON"' EXIT

oci --profile "$PROFILE" --region "$REGION" management-agent agent list \\
  --compartment-id "$COMPARTMENT_ID" \\
  --all \\
  --output json >"$TMP_JSON"

python - "$TMP_JSON" "$AGENT_DISPLAY_NAME" "$AGENT_HOSTNAME_HINT" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
display = sys.argv[2].strip().lower()
host_hint = sys.argv[3].strip().lower()
items = payload.get("data", {{}}).get("items", [])

def norm(value):
    return str(value or "").strip().lower()

matches = []
for item in items:
    name = norm(item.get("display-name"))
    host = norm(item.get("host"))
    if display and name == display:
        matches.append(item)
    elif host_hint and host == host_hint:
        matches.append(item)

if not matches:
    for item in items:
        name = norm(item.get("display-name"))
        host = norm(item.get("host"))
        if display and display in name:
            matches.append(item)
        elif host_hint and host_hint in host:
            matches.append(item)

if not matches:
    raise SystemExit("FAIL no Management Agent matched the display-name/host hint")

item = matches[0]
agent_id = item.get("id", "")
print("Matched Management Agent:")
print(f"  display-name: {{item.get('display-name', '')}}")
print(f"  host: {{item.get('host', '')}}")
print(f"  id: {{agent_id}}")
print("")
print("Config snippet:")
print(f"management_agent_id: {{agent_id}}")
print(f"logan_management_agent_id: {{agent_id}}")
PY
"""


def render_agent_ansible_bootstrap_script(target: Target) -> str:
    target_slug = _slug(target.name)
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ANSIBLE_VENV="${{ANSIBLE_VENV:-$SCRIPT_DIR/.{target_slug}-ansible-venv}}"
PYTHON_BIN="${{PYTHON_BIN:-python3}}"

{_shell_ui()}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 3 is required to bootstrap ansible-core"

if [ ! -d "$ANSIBLE_VENV" ]; then
  info "Creating ansible virtualenv at $ANSIBLE_VENV"
  "$PYTHON_BIN" -m venv "$ANSIBLE_VENV"
fi

info "Installing ansible-core into $ANSIBLE_VENV"
"$ANSIBLE_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$ANSIBLE_VENV/bin/python" -m pip install --upgrade ansible-core

ok "ansible-core is ready"
printf 'Use %s\\n' "$ANSIBLE_VENV/bin/ansible-playbook"
"""


def render_agent_ansible_cfg() -> str:
    return """[defaults]
host_key_checking = True
retry_files_enabled = False
stdout_callback = default
interpreter_python = auto_silent

[ssh_connection]
pipelining = True
"""


def render_agent_ansible_playbook(target: Target, install_script_name: str, verify_script_name: str) -> str:
    return f"""---
- name: Install OCI Management Agent for {target.name}
  hosts: dbman_opsi_targets
  gather_facts: true
  become: true
  vars:
    remote_workdir: /tmp/dbman-opsi-mgmt-agent
    remote_agent_rpm: "{{{{ remote_workdir }}}}/agent.rpm"
    remote_install_script: "{{{{ remote_workdir }}}}/install-agent.sh"
    remote_verify_script: "{{{{ remote_workdir }}}}/verify-agent.sh"
    remote_install_key_file: "{{{{ remote_workdir }}}}/install-key.rsp"
    skip_install: "{{{{ skip_install | default(false) }}}}"
    agent_script_path: "{{{{ playbook_dir }}}}/{install_script_name}"
    verify_script_path: "{{{{ playbook_dir }}}}/{verify_script_name}"

  pre_tasks:
    - name: Validate required controller-side inputs
      ansible.builtin.assert:
        that:
          - ((agent_rpm is defined) and (agent_rpm | length > 0)) or ((agent_rpm_url is defined) and (agent_rpm_url | length > 0))
          - ((agent_rpm_url | default('') | length) == 0) or ((agent_rpm_sha256 is defined) and (agent_rpm_sha256 | length == 64))
          - install_key_file is defined
          - install_key_file | length > 0
        fail_msg: Set AGENT_RPM or AGENT_RPM_URL, and INSTALL_KEY_FILE before running the Ansible wrapper.

    - name: Check local Management Agent RPM
      ansible.builtin.stat:
        path: "{{{{ agent_rpm }}}}"
      delegate_to: localhost
      become: false
      register: agent_rpm_stat
      when: (agent_rpm is defined) and (agent_rpm | length > 0)

    - name: Check local install-key response file
      ansible.builtin.stat:
        path: "{{{{ install_key_file }}}}"
      delegate_to: localhost
      become: false
      register: install_key_stat

    - name: Fail when required local artifacts are missing
      ansible.builtin.assert:
        that:
          - ((agent_rpm is defined) and (agent_rpm | length > 0) and agent_rpm_stat.stat.exists) or ((agent_rpm_url is defined) and (agent_rpm_url | length > 0))
          - install_key_stat.stat.exists
        fail_msg: AGENT_RPM must point to an existing local file unless AGENT_RPM_URL is supplied, and INSTALL_KEY_FILE must exist.

  tasks:
    - name: Create remote staging directory
      ansible.builtin.file:
        path: "{{{{ remote_workdir }}}}"
        state: directory
        mode: "0750"

    - name: Copy Management Agent RPM
      ansible.builtin.copy:
        src: "{{{{ agent_rpm }}}}"
        dest: "{{{{ remote_agent_rpm }}}}"
        mode: "0644"
      when:
        - not (skip_install | bool)
        - (agent_rpm is defined) and (agent_rpm | length > 0)

    - name: Download Management Agent RPM on target host
      ansible.builtin.get_url:
        url: "{{{{ agent_rpm_url }}}}"
        dest: "{{{{ remote_agent_rpm }}}}"
        mode: "0644"
        checksum: "sha256:{{{{ agent_rpm_sha256 }}}}"
      when:
        - not (skip_install | bool)
        - (agent_rpm_url is defined) and (agent_rpm_url | length > 0)

    - name: Copy install-key response file
      ansible.builtin.copy:
        src: "{{{{ install_key_file }}}}"
        dest: "{{{{ remote_install_key_file }}}}"
        mode: "0600"
      when: not (skip_install | bool)

    - name: Copy generated install script
      ansible.builtin.copy:
        src: "{{{{ agent_script_path }}}}"
        dest: "{{{{ remote_install_script }}}}"
        mode: "0750"
      when: not (skip_install | bool)

    - name: Copy generated verify script
      ansible.builtin.copy:
        src: "{{{{ verify_script_path }}}}"
        dest: "{{{{ remote_verify_script }}}}"
        mode: "0750"

    - name: Run generated Management Agent installer
      ansible.builtin.shell: >
        INSTALL_KEY_FILE={{{{ remote_install_key_file | quote }}}}
        DELETE_INSTALL_KEY_FILE=true
        AGENT_RPM={{{{ remote_agent_rpm | quote }}}}
        bash {{{{ remote_install_script | quote }}}}
      args:
        executable: /bin/bash
      register: install_result
      when: not (skip_install | bool)

    - name: Print installer output
      ansible.builtin.debug:
        var: install_result.stdout_lines
      when:
        - install_result is defined
        - install_result.stdout_lines is defined

    - name: Run generated Management Agent verification
      ansible.builtin.shell: bash {{{{ remote_verify_script | quote }}}}
      args:
        executable: /bin/bash
      register: verify_result
      changed_when: false

    - name: Print verification output
      ansible.builtin.debug:
        var: verify_result.stdout_lines
"""


def render_agent_ansible_run_script(
    target: Target,
    config: EnablementConfig,
    playbook_name: str,
    ansible_cfg_name: str,
    bootstrap_name: str,
    resolve_name: str,
) -> str:
    default_user = "opc" if target.external_os != "windows" else "Administrator"
    host_hint = target.logan_hostname or target.external_host or ""
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAYBOOK="$SCRIPT_DIR/{playbook_name}"
ANSIBLE_CFG="$SCRIPT_DIR/{ansible_cfg_name}"
TARGET_HOST="${{TARGET_HOST:-{host_hint}}}"
TARGET_USER="${{TARGET_USER:-{default_user}}}"
SSH_KEY="${{SSH_KEY:-}}"
INSTALL_KEY_FILE="${{INSTALL_KEY_FILE:-$SCRIPT_DIR/{_slug(target.name)}-mgmt-agent-install-key.rsp}}"
AGENT_RPM="${{AGENT_RPM:-}}"
AGENT_RPM_URL="${{AGENT_RPM_URL:-}}"
AGENT_RPM_SHA256="${{AGENT_RPM_SHA256:-}}"
ANSIBLE_PLAYBOOK_BIN="${{ANSIBLE_PLAYBOOK_BIN:-}}"
JUMP_HOST="${{JUMP_HOST:-}}"
JUMP_USER="${{JUMP_USER:-opc}}"
VERIFY_ONLY="${{VERIFY_ONLY:-false}}"
EXTRA_ARGS="${{EXTRA_ARGS:-}}"

{_shell_ui()}

[ -f "$PLAYBOOK" ] || fail "Playbook not found: $PLAYBOOK"
[ -n "$TARGET_HOST" ] || fail "Set TARGET_HOST"
[ -n "$SSH_KEY" ] || fail "Set SSH_KEY to the private key used for the DB host or collector host"
[ -f "$SSH_KEY" ] || fail "SSH key not found: $SSH_KEY"
if [ -z "$AGENT_RPM" ]; then
  [ -n "$AGENT_RPM_URL" ] || fail "Set AGENT_RPM to a local file or AGENT_RPM_URL to the OCI image URL"
  [ -n "$AGENT_RPM_SHA256" ] || fail "Set AGENT_RPM_SHA256 when AGENT_RPM_URL is used"
else
  [ -f "$AGENT_RPM" ] || fail "AGENT_RPM file not found: $AGENT_RPM"
fi
[ -n "$INSTALL_KEY_FILE" ] || fail "Set INSTALL_KEY_FILE"
[ -f "$INSTALL_KEY_FILE" ] || fail "INSTALL_KEY_FILE not found: $INSTALL_KEY_FILE"

if [ -z "$ANSIBLE_PLAYBOOK_BIN" ]; then
  if command -v ansible-playbook >/dev/null 2>&1; then
    ANSIBLE_PLAYBOOK_BIN="$(command -v ansible-playbook)"
  elif [ -x "$SCRIPT_DIR/.{_slug(target.name)}-ansible-venv/bin/ansible-playbook" ]; then
    ANSIBLE_PLAYBOOK_BIN="$SCRIPT_DIR/.{_slug(target.name)}-ansible-venv/bin/ansible-playbook"
  else
    fail "ansible-playbook not found. Run ./{bootstrap_name} first or set ANSIBLE_PLAYBOOK_BIN."
  fi
fi

INVENTORY_FILE="$(mktemp)"
trap 'rm -f "$INVENTORY_FILE"' EXIT

KNOWN_HOSTS="${{SSH_KNOWN_HOSTS:-$SCRIPT_DIR/{_slug(target.name)}-known_hosts}}"
touch "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"
SSH_COMMON_ARGS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS"
if [ -n "$JUMP_HOST" ]; then
  SSH_COMMON_ARGS="$SSH_COMMON_ARGS -o ProxyJump=${{JUMP_USER}}@${{JUMP_HOST}}"
fi

cat >"$INVENTORY_FILE" <<EOF
[dbman_opsi_targets]
target ansible_host=${{TARGET_HOST}} ansible_user=${{TARGET_USER}} ansible_ssh_private_key_file=${{SSH_KEY}} ansible_ssh_common_args='${{SSH_COMMON_ARGS}}'
EOF

SKIP_INSTALL=false
if [ "$VERIFY_ONLY" = "true" ]; then
  SKIP_INSTALL=true
  warn "VERIFY_ONLY=true: install phase will be skipped"
fi

info "Running Ansible playbook against $TARGET_USER@$TARGET_HOST"
ANSIBLE_CONFIG="$ANSIBLE_CFG" "$ANSIBLE_PLAYBOOK_BIN" -i "$INVENTORY_FILE" "$PLAYBOOK" \\
  -e "agent_rpm=$AGENT_RPM" \\
  -e "agent_rpm_url=$AGENT_RPM_URL" \\
  -e "agent_rpm_sha256=$AGENT_RPM_SHA256" \\
  -e "install_key_file=$INSTALL_KEY_FILE" \\
  -e "skip_install=$SKIP_INSTALL" \\
  $EXTRA_ARGS

ok "Ansible Management Agent run completed for {target.name}"
info "Next: run ./{resolve_name} and record the returned OCID in the ignored config."
"""


def _write_ansible_bundle(
    destination: Path,
    target: Target,
    config: EnablementConfig,
    *,
    bootstrap_name: str,
    run_name: str,
    playbook_name: str,
    ansible_cfg_name: str,
    package_name: str,
    install_script_name: str,
    verify_script_name: str,
    resolve_name: str,
) -> list[Path]:
    files = [
        _write_file(destination / bootstrap_name, render_agent_ansible_bootstrap_script(target), executable=True),
        _write_file(destination / ansible_cfg_name, render_agent_ansible_cfg(), executable=False),
        _write_file(destination / package_name, render_agent_package_url_script(target, config), executable=True),
        _write_file(
            destination / playbook_name,
            render_agent_ansible_playbook(target, install_script_name, verify_script_name),
            executable=False,
        ),
        _write_file(
            destination / run_name,
            render_agent_ansible_run_script(target, config, playbook_name, ansible_cfg_name, bootstrap_name, resolve_name),
            executable=True,
        ),
    ]
    return files


def _write_file(path: Path, content: str, *, executable: bool) -> Path:
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o750)
    return path


def generate_agent_scripts(config: EnablementConfig, output_dir: str | Path) -> list[Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for target in config.targets:
        if not _needs_management_agent(target):
            continue
        if target.external_os == "windows":
            install = destination / f"{_slug(target.name)}-agent.ps1"
            paths.append(_write_file(install, render_agent_script(target, config), executable=False))
            paths.append(
                _write_file(
                    destination / _operator_script_name(target, "agent-create-install-key"),
                    render_agent_install_key_script(target, config),
                    executable=True,
                )
            )
            paths.append(
                _write_file(
                    destination / _operator_script_name(target, "agent-resolve"),
                    render_agent_resolve_script(target, config),
                    executable=True,
                )
            )
            continue
        install = destination / _host_script_name(target, "agent")
        verify = destination / _host_script_name(target, "agent-verify")
        install_key = destination / _operator_script_name(target, "agent-create-install-key")
        resolve = destination / _operator_script_name(target, "agent-resolve")
        paths.append(_write_file(install, render_agent_script(target, config), executable=True))
        paths.append(_write_file(verify, render_agent_verify_script(target, config), executable=True))
        paths.append(_write_file(install_key, render_agent_install_key_script(target, config), executable=True))
        paths.append(_write_file(resolve, render_agent_resolve_script(target, config), executable=True))
        paths.extend(
            _write_ansible_bundle(
                destination,
                target,
                config,
                bootstrap_name=f"{_slug(target.name)}-agent-ansible-bootstrap.sh",
                run_name=f"{_slug(target.name)}-agent-ansible-run.sh",
                playbook_name=f"{_slug(target.name)}-agent-ansible-playbook.yml",
                ansible_cfg_name=f"{_slug(target.name)}-agent-ansible.cfg",
                package_name=f"{_slug(target.name)}-agent-resolve-package-url.sh",
                install_script_name=install.name,
                verify_script_name=verify.name,
                resolve_name=resolve.name,
            )
        )
    return paths
