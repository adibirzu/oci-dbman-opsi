import base64
import json

from dbman_opsi._oci_vault import VaultCommands
from dbman_opsi.runner import CommandResult


class FakeRunner:
    dry_run = False

    def run(self, args, **kwargs):
        _ = kwargs
        assert args[-2:] == ["--output", "json"]
        payload = {"data": {"secret-bundle-content": {"content": base64.b64encode(b"vault-value").decode()}}}
        return CommandResult(tuple(args), json.dumps(payload), "", 0)


def test_get_secret_bundle_content_decodes_at_the_explicit_boundary() -> None:
    vault = VaultCommands("DEFAULT", "eu-frankfurt-1", FakeRunner())

    assert vault.get_secret_bundle_content("secret-id") == "vault-value"
