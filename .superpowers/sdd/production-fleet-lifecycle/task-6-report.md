# Task 6 report: Terraform compatibility and security hardening

## Delivered

- Retained `oci_database_management_database_dbm_features_management` as the production DBM resource. Its per-target and master toggles send explicit `false` values for DBM disable, with reviewed CDB/PDB controls for current/future PDB enablement and CDB-driven PDB disablement.
- Removed host-IP inputs and plaintext Data Safe credentials from the production DBM/OPSI module. Production targets now require individual Vault password-secret references, explicit TCP/port/role settings, lifecycle/owner tags where supported, and aggregate-only outputs.
- Added the opt-in `dbm-opsi-compatibility` module using `oci_database_cloud_database_management`. It has `enable_management=false` support, explicit protocol/port/role/Vault/SSL-secret fields, and dependent post-enable managed-database data reads without exporting their identities.
- Added sanitized compatibility and Data Safe demo fixtures. The Data Safe fixture is disabled by default and requires an explicit `allow_plaintext_data_safe_demo=true` acknowledgement; it is the only Terraform path with a plaintext Data Safe password.
- Pinned Terraform/provider constraints to `>= 1.5.0, < 2.0.0` and `oracle/oci >= 6.0.0, < 9.0.0`.
- Added `scripts/security-gate.py`, invoked by pre-push. It rejects committed Terraform state, generated/evidence/journal artifacts, public OCI identifiers, plaintext Terraform password declarations outside loudly gated demos, empty tenant defaults, production host-IP inputs, missing shell-wrapper parents, and shell Terraform destroys without lifecycle ownership binding.

## Verification

- `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q` — 570 passed; 89.26% coverage.
- `/Users/abirzu/oci-cli/bin/python3.11 -m pytest -q tests/evals --no-cov` — 9 passed.
- `terraform fmt -check -recursive` — passed.
- `terraform validate` — passed for the production module, compatibility module, compatibility fixture, Data Safe demo fixture, and existing zero-start fixture using Terraform 1.5.7 and OCI provider 8.24.0.
- `/Users/abirzu/oci-cli/bin/python3.11 scripts/security-gate.py` — passed.

## Constraints

- Validation is schema/static only; no OCI apply or live tenant evidence was performed.
- Existing untracked `docs/plans/` content was preserved.

## Fix round 1

- Confirmed against OCI provider 8.24 schema that `oci_database_cloud_database_management` exposes no Managed Database OCID. Compatibility verification now lists managed databases in the caller compartment by exact `managed_database_name`, retains only exact name/compartment matches, fails Terraform planning when the match count is zero or ambiguous, and passes the computed collection item ID to the singular Managed Database data source.
- Expanded the public-artifact gate to scan every Terraform path plus tracked documentation/evidence/configuration surfaces for OCIDs and RFC1918 topology. `host_ip` is rejected in every Terraform root, module, example, and fixture.
- Narrowed plaintext exceptions to the exact two sanctioned Data Safe demo files. Each declares or uses the plaintext input only with its own named acknowledgement check; an arbitrary adjacent password file fails closed. Removed the other public Terraform plaintext-password inputs and disabled public-stack database provisioning that required them.
- Added scanner bypass regressions for example `host_ip`, evidence topology, and adjacent demo-password files, plus semantic compatibility assertions that distinguish a database OCID from a computed Managed Database ID.
