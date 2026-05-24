from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestSSHProxy(unittest.TestCase):
    """Regression tests for the ssh_known_hosts auto-population fix.

    Background: bench cert SSH was broken because the bastion's
    /etc/ssh/ssh_known_hosts was empty; the forced-command
    `ssh frappe@<ip>:<port>` aborted with "Host key verification failed"
    after user cert auth succeeded.

    The fix added add_known_host (call site in add_user_job) and
    remove_known_host (optional call site in remove_user_job).
    """

    def _make_proxy(self):
        # Bypass SSHProxy.__init__ (reads config.json) so we can unit-test
        # the job orchestration purely with mocks.
        from agent.ssh import SSHProxy

        proxy = SSHProxy.__new__(SSHProxy)
        proxy.directory = "/tmp/fake"
        proxy.ssh_directory = "/tmp/fake/ssh"
        proxy.name = "fake-ssh-proxy"
        proxy.job = None
        proxy.step = None
        return proxy

    def test_add_user_job_runs_add_known_host(self):
        # Given a freshly-faked SSHProxy with all step methods mocked
        proxy = self._make_proxy()
        ssh = {"ip": "10.0.0.5", "port": 22018}
        with patch.object(proxy, "add_user") as add_user, \
             patch.object(proxy, "add_certificate") as add_certificate, \
             patch.object(proxy, "add_principal") as add_principal, \
             patch.object(proxy, "add_known_host") as add_known_host:
            # When add_user_job is invoked (the decorated wrapper still calls
            # the underlying function with self as first arg)
            from agent.ssh import SSHProxy
            SSHProxy.add_user_job.__wrapped__(
                proxy, "bench-X", "bench-group-X", ssh, {"id_rsa": "..."}
            )
        # Then add_known_host must be called with the ssh dict
        add_user.assert_called_once_with("bench-X")
        add_certificate.assert_called_once_with("bench-X", {"id_rsa": "..."})
        add_principal.assert_called_once_with("bench-X", "bench-group-X", ssh)
        add_known_host.assert_called_once_with(ssh)

    def test_remove_user_job_calls_remove_known_host_when_ssh_given(self):
        proxy = self._make_proxy()
        ssh = {"ip": "10.0.0.5", "port": 22018}
        with patch.object(proxy, "remove_user") as remove_user, \
             patch.object(proxy, "remove_principal") as remove_principal, \
             patch.object(proxy, "remove_known_host") as remove_known_host:
            from agent.ssh import SSHProxy
            SSHProxy.remove_user_job.__wrapped__(proxy, "bench-X", ssh=ssh)
        remove_user.assert_called_once_with("bench-X")
        remove_principal.assert_called_once_with("bench-X")
        remove_known_host.assert_called_once_with(ssh)

    def test_remove_user_job_backward_compatible_without_ssh(self):
        # Older Press callers DELETE /ssh/users/<bench> with no body.
        # The agent must still succeed; remove_known_host is skipped.
        proxy = self._make_proxy()
        with patch.object(proxy, "remove_user") as remove_user, \
             patch.object(proxy, "remove_principal") as remove_principal, \
             patch.object(proxy, "remove_known_host") as remove_known_host:
            from agent.ssh import SSHProxy
            SSHProxy.remove_user_job.__wrapped__(proxy, "bench-X")
        remove_user.assert_called_once_with("bench-X")
        remove_principal.assert_called_once_with("bench-X")
        remove_known_host.assert_not_called()

    def test_add_known_host_uses_idempotent_keygen_then_keyscan(self):
        # The new step must be safe to call repeatedly: ssh-keygen -R
        # removes the existing entry first, then ssh-keyscan re-adds it.
        # Bypass the @step decorator (which needs a job_record context) by
        # calling the underlying function via __wrapped__.
        from agent.ssh import SSHProxy

        proxy = self._make_proxy()
        ssh = {"ip": "10.0.0.5", "port": 22018}
        with patch.object(proxy, "docker_execute") as docker_execute:
            SSHProxy.add_known_host.__wrapped__(proxy, ssh)
        commands = [call.args[0] for call in docker_execute.call_args_list]
        assert any("ssh-keygen -R" in c and "[10.0.0.5]:22018" in c for c in commands), (
            f"expected idempotent ssh-keygen -R before scan; got: {commands}"
        )
        assert any("ssh-keyscan -p 22018" in c and "10.0.0.5" in c for c in commands), (
            f"expected ssh-keyscan to append live key; got: {commands}"
        )

    def test_remove_known_host_targets_correct_host_pattern(self):
        from agent.ssh import SSHProxy

        proxy = self._make_proxy()
        ssh = {"ip": "10.0.0.5", "port": 22018}
        with patch.object(proxy, "docker_execute") as docker_execute:
            SSHProxy.remove_known_host.__wrapped__(proxy, ssh)
        docker_execute.assert_called_once()
        cmd = docker_execute.call_args.args[0]
        assert "ssh-keygen -R" in cmd
        assert "[10.0.0.5]:22018" in cmd
        assert "/etc/ssh/ssh_known_hosts" in cmd


if __name__ == "__main__":
    unittest.main()
