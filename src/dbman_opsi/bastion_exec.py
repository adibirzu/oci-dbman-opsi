"""Bastion-based SQL transport for the hybrid DB-side executor.

Implements the proven Bastion procedure as an injectable ``SqlRunner`` for
``db_exec.DbExecService``: create a managed-SSH **port-forwarding** session to the
DB node :22, tunnel a local port through it, ``scp`` each generated script to the
host and run it as ``oracle`` via ``sqlplus / as sysdba``, then tear the session
down. The session is always deleted in a ``finally`` block.

The subprocess calls are injected (``exec_fn``/``exec_bg_fn``/``session_id_fn``)
so the command sequence is unit-tested without real SSH; the defaults shell out.

Note: the generated DB-side scripts use SQL*Plus ``accept`` prompts (so passwords
are never stored). When auto-executing, supply the answers non-interactively via
``answers`` (piped to the script's stdin) — e.g. the PDB/container name and the
monitoring password — in the order the script prompts for them.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dbman_opsi.config import Target


def _default_exec(argv: list[str], input: str | None = None) -> str:  # noqa: A002
    result = subprocess.run(argv, input=input, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv[:3])}…\n{result.stderr}")
    return result.stdout


def _default_exec_bg(argv: list[str]) -> None:
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class BastionSqlRunner:
    def __init__(
        self,
        bastion_id: str,
        target_private_ip: str,
        ssh_key: str,
        profile: str,
        region: str,
        *,
        local_port: int = 8022,
        bastion_host: str | None = None,
        session_ttl: int = 3600,
        remote_dir: str = "/tmp",
        answers: str | None = None,
        known_hosts: str | None = None,
        exec_fn: Callable[..., str] | None = None,
        exec_bg_fn: Callable[[list[str]], None] | None = None,
        session_id_fn: Callable[[], str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        tunnel_wait: float = 6.0,
        atexit_register: Callable[[Callable[[], None]], Any] = atexit.register,
    ) -> None:
        self.bastion_id = bastion_id
        self.target_private_ip = target_private_ip
        self.ssh_key = ssh_key
        self.profile = profile
        self.region = region
        self.local_port = local_port
        self.bastion_host = bastion_host or f"host.bastion.{region}.oci.oraclecloud.com"
        self.session_ttl = session_ttl
        self.remote_dir = remote_dir
        self.answers = answers
        self.known_hosts = known_hosts
        self._exec = exec_fn or _default_exec
        self._exec_bg = exec_bg_fn or _default_exec_bg
        self._session_id_fn = session_id_fn or self._resolve_session_id
        self._sleep = sleeper
        self.tunnel_wait = tunnel_wait
        self._atexit_register = atexit_register

    # SqlRunner protocol: (target, scripts) -> combined output.
    def __call__(self, target: Target, scripts: list[Path]) -> str:
        display_name = f"dbman-exec-{target.name}".replace(" ", "-").lower()[:60]
        self._exec([
            "oci", "--profile", self.profile, "--region", self.region,
            "bastion", "session", "create-port-forwarding",
            "--bastion-id", self.bastion_id,
            "--display-name", display_name,
            "--ssh-public-key-file", f"{self.ssh_key}.pub",
            "--target-private-ip", self.target_private_ip,
            "--target-port", "22",
            "--session-ttl", str(self.session_ttl),
            "--wait-for-state", "SUCCEEDED",
            "--max-wait-seconds", "600", "--wait-interval-seconds", "15",
        ])
        session_id = self._session_id_fn()
        known_hosts, kh_owned = self._known_hosts_path()
        ssh_opts = self._ssh_opts(known_hosts)

        # Idempotent teardown: registered with atexit so an interpreter exit (or an
        # unhandled signal that surfaces as an exception) still deletes the session,
        # not just the normal finally below. SIGKILL can't be caught — the session
        # TTL is the backstop for that. The guard makes the atexit + finally calls
        # collapse to a single delete.
        torn = {"done": False}

        def _safe_teardown() -> None:
            if torn["done"]:
                return
            torn["done"] = True
            self._teardown(session_id)

        self._atexit_register(_safe_teardown)

        remote_paths: list[str] = []
        outputs: list[str] = []
        try:
            self._exec_bg([
                "ssh", "-i", self.ssh_key, "-fNL",
                f"{self.local_port}:{self.target_private_ip}:22", "-p", "22",
                f"{session_id}@{self.bastion_host}",
                "-o", "ExitOnForwardFailure=yes", *ssh_opts,
            ])
            self._sleep(self.tunnel_wait)
            for script in scripts:
                remote = f"{self.remote_dir}/{script.name}"
                remote_paths.append(remote)
                self._exec([
                    "scp", "-i", self.ssh_key, "-P", str(self.local_port), *ssh_opts,
                    str(script), f"opc@127.0.0.1:{remote}",
                ])
                outputs.append(self._exec([
                    "ssh", "-i", self.ssh_key, "-p", str(self.local_port), *ssh_opts,
                    "opc@127.0.0.1",
                    f"sudo su - oracle -c 'sqlplus -s / as sysdba @{remote}'",
                ], input=self.answers))
        finally:
            # Remove the uploaded scripts while the tunnel is still up (before
            # teardown), best-effort so a failed run still doesn't leave residue.
            self._cleanup_remote(ssh_opts, remote_paths)
            _safe_teardown()
            if kh_owned:
                try:
                    Path(known_hosts).unlink()
                except OSError:
                    pass
        return "\n".join(outputs)

    def _cleanup_remote(self, ssh_opts: list[str], remote_paths: list[str]) -> None:
        """Best-effort removal of uploaded scripts from the DB host's /tmp."""

        if not remote_paths:
            return
        try:
            self._exec([
                "ssh", "-i", self.ssh_key, "-p", str(self.local_port), *ssh_opts,
                "opc@127.0.0.1", "rm -f " + " ".join(remote_paths),
            ])
        except Exception:  # noqa: BLE001 - tunnel may be down; nothing else to do
            pass

    def _ssh_opts(self, known_hosts: str) -> list[str]:
        """TOFU host-key options shared by the tunnel, scp and ssh hops.

        ``accept-new`` records the host key on first contact into ``known_hosts``
        and *verifies* it for every subsequent hop in the same run — closing the
        loopback-tunnel MITM gap that ``StrictHostKeyChecking=no`` left open. The
        file is per-run because the loopback tunnel (127.0.0.1:local_port) maps to
        a different DB host each run; a persistent file would reject the new key as
        a host-key change.
        """

        return [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=20",
        ]

    def _known_hosts_path(self) -> tuple[str, bool]:
        """Return ``(path, owned)``; create a 0600 per-run file when unset."""

        if self.known_hosts:
            return self.known_hosts, False
        fd, path = tempfile.mkstemp(prefix="dbman-knownhosts-")
        os.close(fd)
        os.chmod(path, 0o600)
        return path, True

    def _teardown(self, session_id: str) -> None:
        try:
            self._exec([
                "oci", "--profile", self.profile, "--region", self.region,
                "bastion", "session", "delete", "--session-id", session_id, "--force",
            ])
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass

    def _resolve_session_id(self) -> str:
        # Default resolver: list active sessions and return the most recent id.
        import json

        raw = self._exec([
            "oci", "--profile", self.profile, "--region", self.region,
            "bastion", "session", "list", "--bastion-id", self.bastion_id, "--all",
            "--query", "data[?\"lifecycle-state\"=='ACTIVE']|[0].id",
            "--raw-output", "--output", "json",
        ])
        value = raw.strip()
        if value.startswith('"'):
            value = json.loads(value)
        return value
