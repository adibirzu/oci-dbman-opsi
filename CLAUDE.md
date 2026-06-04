# dbman-opsi

End-to-end OCI **Database Management** and **Operations Insights** enablement for
DBCS / Base Database, Autonomous Database, Exadata Database Service, and external
databases (via OCI Management Agents). Public-repo-ready workshop toolkit.

Runs from OCI Cloud Shell, a local workstation, or OCI Resource Manager. Every
tenant-specific value comes from variables, gitignored local config, OCI Vault, or
env vars — never hardcoded.

## Commands

| Task | Command |
|------|---------|
| Install (editable) | `pip install -e .` |
| Install dev extras | `pip install -e '.[dev]'` |
| Test (enforces ≥80% coverage) | `pytest` |
| Run CLI | `dbman-opsi` |

`pytest` config lives in `pyproject.toml` (`--cov=dbman_opsi --cov-fail-under=80`,
`pythonpath=["src"]`). No lint/format tool is configured in this repo.

## Layout

- `src/dbman_opsi/` — Python package. Logical enablement workflow:
  - `orchestrator.py` — drives the end-to-end flow
  - `discovery.py` · `preflight.py` · `prerequisites.py` · `checks.py` · `db_check.py` — is-it-installed / is-it-ready gates
  - `enablement.py` · `iam.py` · `handoff.py` — turn on DB Management / Ops Insights, IAM, registration
  - `oci_cli.py` · `terraform.py` · `tf_outputs.py` · `runner.py` — OCI CLI + Terraform execution
  - `wizard.py` · `cli.py` · `config.py` · `validation.py` · `status.py` · `doctor.py` · `reporting.py` — UX, config, status, reporting
  - `redact.py` — strips OCIDs/IPs/secrets from output
- `tests/` — pytest suite, mirrors module names
- `terraform/examples/zero-start-poc/` — provisions a DBCS in an existing VCN/subnet for testing
- `docs/workshop/` — workshop guide

## OCI tenancy rules (MANDATORY)

See `~/.claude/CLAUDE.md` for the full tenancy matrix. Short form:

- `cap` (staging tenancy, eu-frankfurt-1) — **full control**. Use for testing/experiments.
- `emdemo` — **production, read-only** outside the `LogAnalytics` compartment.
- `DEFAULT` (oci4cca) — personal scratch.

Never inline real OCIDs, public IPs, tenancy namespaces, or datakeys in committed
files — use `<PLACEHOLDER>` tokens.

## Gotcha

- OCI **PDB** DB Management requires the parent **CDB** management-type set to
  `ADVANCED`. Enable CDB ADVANCED before registering a PDB.

## Troubleshooting

On error only, consult `KB.md` (this repo). Add a new KB entry after fixing any
new error.
