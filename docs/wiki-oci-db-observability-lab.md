# OCI DB Management and Operations Insights Wiki

This file is the repository-side source for the GitHub Wiki. The canonical
operator documentation is the [Production Operations Guide](production-operations-guide.md).
It describes the latest product capabilities, installation, configuration,
options, lifecycle operations, security controls, verification, and release
gates.

The GitHub Wiki is deliberately page-oriented:

1. **Home** — capabilities, scope, and operating boundary.
2. **Getting Started** — installation and OCI authentication.
3. **Configuration** — target configuration, fleet answers, filters, and options.
4. **Fleet Lifecycle** — plan, apply, resume, signed handoffs, status, and cleanup.
5. **Operations** — service-specific runbooks, incident evidence, and diagnostics.
6. **Security and Release** — secret/state handling, local verification, and live-evidence gates.

## Capability summary

- Read-only discovery across subscribed regions and accessible compartments.
- Per-target enablement for DBM, OPSI, Data Safe, and Log Analytics.
- Plan-gated fleet onboarding with exact approval IDs, checkpoints, and resume.
- Per-account OCI Vault credential references; production rejects shared
  passwords.
- Signed DBA/host-admin handoff and signed collection-evidence import.
- Redacted journals and status output; registration is never presented as a
  collection-proof result.
- Ownership-safe reverse offboarding; production never deletes databases.
- Bounded DB incident evidence, OPSI diagnostic packets, and Process Insights
  diagnostics.

## Reading paths

Use the [Production Operations Guide](production-operations-guide.md) for the
complete operator path. Use the [Fleet Lifecycle Operator Runbook](fleet-lifecycle-runbook.md)
for the immutable-plan contract and scratch-tenancy acceptance matrix. Use the
[Workshop](workshop/README.md), [DB incident demo runbook](demo-db-incident-e2e.md),
and [Data Safe to Log Analytics guide](datasafe-log-analytics.md) only for
approved disposable demonstrations.

This is not an official Oracle product or Oracle-supported deployment tool.
Production use requires approved access, change control, and current redacted
live evidence. Keep tenant identifiers, hostnames, IPs, connect strings,
passwords, wallets, and secrets in ignored local files or OCI Vault.
