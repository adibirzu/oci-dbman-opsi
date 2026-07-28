# Getting Started

## Install

Run from OCI Cloud Shell, a controlled workstation, or an approved automation
runner. Python 3.11+, OCI CLI, and an authenticated OCI identity are required.
Terraform 1.5+ is required only for Terraform provisioning paths.

```bash
git clone https://github.com/adibirzu/oci-dbman-opsi.git
cd oci-dbman-opsi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.local.example .env.local
chmod 600 .env.local
dbman-opsi doctor --profile <OCI_PROFILE> --region <OCI_REGION>
```

Cloud Shell already includes OCI CLI. Use its signed-in `DEFAULT` profile or an
approved profile. Keep `.env.local` private; do not put a password, wallet, OCI
identifier, endpoint, or target topology into source control.

## Authenticate lifecycle commands

```bash
# Named profile (default)
dbman-opsi onboard --profile <PROFILE> --region <REGION> ...

# One mutually exclusive principal mode
dbman-opsi onboard --region <REGION> --security-token ...
dbman-opsi onboard --region <REGION> --instance-principal ...
dbman-opsi onboard --region <REGION> --resource-principal ...
```

Grant only the approved scope. DB, host, Vault, private endpoint, and Management
Agent actions have separate ownership boundaries. Begin an incident diagnostic
with read-only scope whenever possible.
