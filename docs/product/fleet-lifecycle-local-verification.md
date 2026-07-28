# Fleet Lifecycle Local Verification Receipt

Receipt date: 2026-07-27. Implementation baseline: `f382129`. This receipt records
the final integrated local verification run; no claim is made that a commit or local
test proves live OCI behavior.

## Local evidence

| Check | Command | Result |
| --- | --- | --- |
| Python suite and coverage | `pytest -q` | 621 passed; 89.86% total coverage; required 80% gate passed |
| Eval fence | `pytest -q -m eval --no-cov` | 9 passed, 612 deselected |
| Docs/public readiness | `pytest -q --no-cov tests/test_docs_links.py tests/test_public_repo_readiness.py` | 8 passed |
| Security and diff | `python scripts/security-gate.py`; `git diff --check` | passed |
| Terraform formatting | `terraform fmt -check -recursive` | passed |
| Terraform validation | `terraform validate` in `terraform/examples/data-safe-plaintext-demo`, `terraform/examples/zero-start-poc`, `terraform/fixtures/dbm-opsi-compatibility`, `terraform/modules/dbm-opsi-compatibility`, and `terraform/modules/dbm-opsi-enablement` | all passed |
| Scale acceptance | `pytest -q --no-cov tests/test_fleet_executor.py::test_fake_fleet_plan_execute_status_and_empty_offboard_scale_without_topology` | 1, 100, and 1000 targets passed through the real executor with in-memory checkpoints and one validation phase; full nine-phase ordering and SQLite durability are separate focused tests |

The one-process suite completed with coverage instrumentation. The 1/100/1000
scale contract executes the real fleet executor with in-memory checkpoints and one
validation phase; full nine-phase ordering and SQLite durability remain covered by
separate focused tests.

## Evidence boundary

This is local implementation evidence only. It contains no scratch-tenancy
execution, customer or production credential, OCI target, control-plane collection
query, live cleanup, release approval, or redacted live receipt. Every live
scratch-tenancy and release gate remains **IN PROGRESS / OWNER INPUT REQUIRED**.
See the [fleet lifecycle runbook](../fleet-lifecycle-runbook.md) for the required
authority, target-family matrix, signed handoffs, and redacted evidence contract.
