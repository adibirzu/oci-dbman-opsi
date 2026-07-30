# OCI DB Management and Operations Insights

`dbman-opsi` enables and operates OCI Database Management (DBM), Operations
Insights (OPSI), Data Safe, and Log Analytics for DBCS CDB/PDB, Autonomous
Database, Exadata, external databases, and external Exadata.

It provides two complementary paths:

- expert per-target discovery, prerequisite checks, enablement, and diagnostics;
- a production fleet lifecycle: read-only discovery, immutable reviewed plan,
  exact approval, checkpointed resume, signed handoffs, collection-proof gates,
  and ownership-safe offboarding.

## Production boundary

The tool is designed to support controlled production operations, but it is not
an official Oracle product or Oracle-supported deployment tool. Production
changes require approved identity/policies, target-owner authority, a reviewed
change record, and current redacted live evidence. Passing tests, a successful
registration, or a `configured` result is not proof that collection is ready.

## Capabilities

| Capability | Result |
| --- | --- |
| Whole-fleet discovery | Reads subscribed regions and accessible compartments; failed scope reads block planning. |
| Service selection | Targets opt into `dbm`, `opsi`, `datasafe`, and/or `logan`. |
| Credential safety | Vault references and per-account credentials; production rejects shared passwords. |
| Lifecycle control | Exact plan IDs, checkpoints, bounded retry, authorization circuit breaker, and resume. |
| Collection readiness | Keeps configured, collecting, ready, degraded, blocked, and handed-off states separate. |
| Safe cleanup | Removes only run-owned and run-enabled resources; production never deletes databases. |
| Operations | Redacted journal, bounded DB incident bundles, Process Insights, and OPSI failure packets. |
| Scale and Landing Zones | The same immutable-plan workflow is locally acceptance-covered at 1, 100, and 1,000 targets; a dedicated Terraform module provides DBM/OPSI Landing Zone foundations. |

Start with [[Getting Started]], then [[Configuration]], [[Fleet Lifecycle]], and
[[Scale and Landing Zones]]. The current live boundary is recorded in
[[CAP Canary Validation]].
