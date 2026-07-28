# PRD: SQLcl MCP Integration

Version: 1.0 · Owner: Luna · Task: L-01 · Status: In progress

## Outcome

SQLcl is the database MCP surface for investigation while OCI control-plane actions remain in this CLI. It connects only as `MCP_READONLY` using an OCI Vault reference, never a credential in configuration.

## Requirements

- Validate supported Java and SQLcl releases during setup.
- Generate a template containing an approved connect descriptor, `MCP_READONLY`, and `DBMAN_OPSI_SECRET_ID` only.
- Retrieve the secret only in the authorized launcher/process boundary.
- Database grants are enforcement; a one-statement SELECT/CTE/EXPLAIN guard is defence in depth.
- Reject DDL/DML/PLSQL, multi-statement input, non-approved users, and connection targets.

## Acceptance

- [ ] Templates contain no password, wallet material, or private key.
- [ ] Approved troubleshooting queries work through `MCP_READONLY`.
- [ ] Write and unapproved-connection attempts are rejected.

Operator command: `dbman-opsi generate-sqlcl-mcp --connect-descriptor <CONNECT_DESCRIPTOR> --secret-id <MCP_READONLY_SECRET_OCID>`.
