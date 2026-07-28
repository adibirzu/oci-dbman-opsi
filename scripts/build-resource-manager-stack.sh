#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
source_root="${repo_root}/terraform/examples/zero-start-poc"
module_root="${repo_root}/terraform/modules/dbm-opsi-enablement"
output_root="${1:-${repo_root}/dist/resource-manager-stack}"

case "${output_root}" in
  ""|"/"|"${repo_root}"|"${source_root}"|"${module_root}")
    echo "Refusing unsafe Resource Manager output path: ${output_root}" >&2
    exit 2
    ;;
esac

if [[ -e "${output_root}" ]]; then
  echo "Output path already exists; choose an empty destination: ${output_root}" >&2
  exit 2
fi

mkdir -p "${output_root}/modules/dbm-opsi-enablement"

# Resource Manager requires Terraform and schema.yaml at the archive root.
# Rewrite only the local module path in the copied main.tf so the canonical
# workstation layout and the self-contained ORM package can share one source.
sed \
  's#source = "../../modules/dbm-opsi-enablement"#source = "./modules/dbm-opsi-enablement"#' \
  "${source_root}/main.tf" > "${output_root}/main.tf"

cp "${source_root}/variables.tf" "${output_root}/variables.tf"
cp "${source_root}/versions.tf" "${output_root}/versions.tf"
cp "${source_root}/schema.yaml" "${output_root}/schema.yaml"
cp "${source_root}/.terraform.lock.hcl" "${output_root}/.terraform.lock.hcl"
cp "${module_root}/main.tf" "${output_root}/modules/dbm-opsi-enablement/main.tf"
cp "${module_root}/variables.tf" "${output_root}/modules/dbm-opsi-enablement/variables.tf"
cp "${module_root}/outputs.tf" "${output_root}/modules/dbm-opsi-enablement/outputs.tf"
cp "${module_root}/versions.tf" "${output_root}/modules/dbm-opsi-enablement/versions.tf"
cp "${repo_root}/docs/resource-manager.md" "${output_root}/README.md"

if grep -n '\.\./\.\./modules/dbm-opsi-enablement' "${output_root}/main.tf" >/dev/null; then
  echo "Packaged module source still points outside the archive." >&2
  exit 3
fi

if find "${output_root}" \( -name '*.tfstate' -o -name '*.tfstate.*' -o -name '.terraform' -o -name 'terraform.tfvars*' \) -print -quit | grep -q .; then
  echo "Forbidden Terraform runtime or state material entered the package." >&2
  exit 3
fi

(
  cd "${output_root}"
  find . -type f -print | LC_ALL=C sort > PACKAGE-MANIFEST.txt
)

echo "Resource Manager stack assembled at ${output_root}"
