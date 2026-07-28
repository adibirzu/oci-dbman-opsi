# Demo and PoC Use Notice

`dbman-opsi` is a community demo and proof-of-concept toolkit. It is **not an
official Oracle product**, is not supported as an Oracle service, and must not
be represented as one.

Use it only in a disposable, customer-approved non-production compartment. It
can help sales, ACE, CAM, and technical teams demonstrate the workflow around
Database Management, Operations Insights, Data Safe, and Log Analytics; it is
not a production deployment accelerator or a substitute for security, network,
licensing, change-control, or Oracle Support review.

The interactive plan flow asks for the OCI profile, region, compartment,
database target type, network choice, and desired pillars. Keep all tenancy
values in ignored local config and environment files. Before applying, review
the Terraform plan, costs, service limits, IAM policies, and the generated
teardown plan. Reuse individual generated scripts only after adapting and
reviewing them for the target environment.

For a complete demonstration, use a dedicated database, a separate approved
execution path to the DB host, OCI Vault-backed credentials, and a lifecycle
tag unique to the demo. Never use a production database or shared credentials.
