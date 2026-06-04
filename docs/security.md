# Security And Public Repository Guidance

This project is designed for public repository use. Keep tenant-specific data in ignored local files and use variables for every deploy-time value.

## Public-Safe Defaults

- Do not commit generated configs, generated SQL payloads, Terraform state, local logs, screenshots, or local MCP files.
- Do not commit OCIDs, public IPs, private IPs, API key fingerprints, namespaces, endpoint URLs, wallet material, or passwords.
- Use OCI Vault for database monitoring credentials.
- Use environment variables for secret input, for example `DBMAN_OPSI_DB_PASSWORD`, then run `prepare-prereqs --password-env DBMAN_OPSI_DB_PASSWORD`.
- Use placeholders in documentation and workshops, such as `<COMPARTMENT_OCID>` and `<PRIVATE_SUBNET_OCID>`.

## Screenshot Rules

Screenshots for workshops must not show tenant names, user names, tenancy OCIDs, compartment OCIDs, database OCIDs, public IP addresses, private IP addresses, or credential values. Crop or mask the browser chrome and account selector before publishing.

## Validation Before Publishing

Run:

```bash
python3 -m pytest
terraform -chdir=terraform/examples/zero-start-poc fmt -check
rg -n 'ocid1\.|<personal-name>|<tenant-name>|130\.61|161\.153' README.md docs terraform src tests
```

The final `rg` command should return no public sensitive values.
