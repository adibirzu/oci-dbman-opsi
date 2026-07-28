# Deploy with OCI Resource Manager

The Deploy to Oracle Cloud path creates or reuses OCI-side prerequisites for
the `dbman-opsi` fleet lifecycle. It is intentionally not a second onboarding
engine: discovery, database selection, credential bindings, database/host
handoffs, service enablement, collection proof, and ownership-safe service
offboarding remain in the CLI.

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/adibirzu/oci-dbman-opsi/archive/refs/heads/resource-manager-stack.zip)

## What the stack manages

| Selection | Created when no existing reference is supplied | Completed later by the CLI |
| --- | --- | --- |
| DBM | Database Management private endpoint | Target discovery, Vault credential binding, CDB/PDB enablement, preferred credentials, readiness |
| OPSI | Database Management and Operations Insights private endpoints | Database Insight enablement and collection proof |
| Data Safe | Data Safe private endpoint only when explicitly requested | Regional/tenancy enablement, target registration, database credential handling |
| Log Analytics | No agent or source association is created by this prerequisite stack | Namespace checks/onboarding, Management Agent, entities, source associations, searchable-record proof |
| Vault | Lifecycle-owned Vault and AES key, or reviewed existing references | Secret creation/version checks and per-target credential policy |
| Network | PoC/Demo VCN, private subnet, service gateway, route table, and security list, or reviewed existing references | Database-specific routing, NSGs, host firewall, Bastion, and collector authority |

The stack never accepts a database password. It does not provision databases,
create database users, execute SQL, register targets, or claim that collection
is ready.

## Mode behavior

| Mode | Resource Manager behavior |
| --- | --- |
| `poc` | May create lifecycle-tagged networking, Vault/key, and selected private endpoints |
| `demo` | Same prerequisite choices as PoC; use a unique lifecycle ID and preserve teardown evidence |
| `production` | Fails planning if disposable network creation is selected; requires an existing reviewed VCN and private subnet |

Production still permits a reviewed, Terraform-owned private endpoint or
Vault/key when the owner wants Resource Manager to manage that prerequisite.
The CLI never deletes a production database, and Resource Manager destroys only
resources present in its own state.

## Before creating the stack

Confirm:

- the target tenancy, region, and resource compartment;
- the Resource Manager principal has the required network, Vault/KMS, DBM, OPSI,
  and optional Data Safe permissions;
- existing VCN/subnet and Vault/key ownership when reuse is selected;
- Data Safe is already enabled in the region before requesting its private
  endpoint;
- the CIDRs do not overlap existing networks;
- a unique lifecycle ID is available for this run.

IAM policy creation is disabled by default. This avoids silently creating a
tenancy-level policy from a public quick-start form. Apply owner-reviewed IAM
outside this stack or use the expert CLI-generated Terraform variables after
reviewing every statement.

## Create, plan, and apply

1. Select the Deploy to Oracle Cloud button.
2. Sign in to the intended tenancy and select the target region.
3. Set a unique lifecycle ID and choose `poc`, `demo`, or `production`.
4. Select service prerequisites.
5. Choose lifecycle-owned or existing networking and Vault resources.
6. For production, clear **Run apply** during stack creation.
7. Create the stack and run a **Plan** job.
8. Review every create, update, replacement, and destroy action.
9. Apply from the reviewed plan job according to your change-control process.

Do not place secrets, private keys, database passwords, host addresses, or
unreviewed IAM statements in Resource Manager variables. Terraform state
contains topology and resource references even though the schema disables
state display in the Application Information surface.

## Continue with fleet onboarding

After apply, use the Resource Manager outputs as private references when
creating the CLI answers/bindings:

- VCN and subnet;
- Database Management private endpoint;
- Operations Insights private endpoint;
- optional Data Safe private endpoint;
- Vault and key;
- lifecycle ownership receipt.

Then follow the
[fleet quick start](https://github.com/adibirzu/oci-dbman-opsi#fleet-quick-start):

```text
onboard --plan-only → approve exact plan ID → onboard → fleet-status
```

Keep the output mapping in an ignored `0600` file. The CLI questionnaire and
whole-tenancy discovery determine the exact databases; the Resource Manager
stack does not contain the selected fleet.

## Offboarding order

Do not destroy prerequisites while services still depend on them:

1. Run `dbman-opsi offboard --plan-only`.
2. Approve and execute ownership-safe service offboarding.
3. Complete any DBA/host cleanup handoffs.
4. Verify that run-owned service resources are gone and reused resources remain.
5. Run a Resource Manager destroy **plan** and review it.
6. Destroy the stack-created private endpoints, Vault/key, and disposable
   network only after the dependency check passes.

Production mode never authorizes database deletion. Resource Manager cannot
delete databases because this package does not create them.

## Package integrity and local validation

The README button downloads the generated `resource-manager-stack` branch, not
the repository source archive. OCI requires Terraform files and `schema.yaml`
at the package root. The generated branch contains only:

- root Terraform configuration and provider lock file;
- `schema.yaml`;
- the vendored local DBM/OPSI enablement module;
- this operator guide and a package manifest.

It excludes Terraform state, `.terraform`, variable files, credentials, Python
source, screenshots, and tenancy evidence. Pull requests build and validate the
same package. A successful merge to `main` republishes the package branch only
after validation succeeds.

Build and validate the exact package locally:

```bash
package_dir="$(mktemp -d)/resource-manager-stack"
scripts/build-resource-manager-stack.sh "${package_dir}"
python scripts/validate_resource_manager_schema.py "${package_dir}"
terraform -chdir="${package_dir}" fmt -check -recursive
terraform -chdir="${package_dir}" init -backend=false -input=false
terraform -chdir="${package_dir}" validate
```

The build fails if the destination already exists or if Terraform state,
runtime directories, or variable files enter the package.

## Official OCI references

- [Using the Deploy to Oracle Cloud button](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Tasks/deploybutton.htm)
- [Resource Manager schema documents](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm)
- [Resource Manager](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/home.htm)
- [Operations Insights private endpoint Terraform resource](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/opsi_operations_insights_private_endpoint.html)
- [Data Safe private endpoint Terraform resource](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/data_safe_data_safe_private_endpoint.html)
