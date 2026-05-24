from __future__ import annotations

import os
import tempfile

from agent.job import job, step
from agent.server import Server


class SSHProxy(Server):
    def __init__(self, directory=None):
        super().__init__(directory=directory)

        self.directory = directory or os.getcwd()
        self.config_file = os.path.join(self.directory, "config.json")
        self.name = self.config["name"]

        self.ssh_directory = os.path.join(self.directory, "ssh")

        self.job = None
        self.step = None

    def docker_execute(self, command):
        command = f"docker exec ssh {command}"
        return self.execute(command)

    @job("Add User to Proxy")
    def add_user_job(self, name, principal, ssh, certificate):
        self.add_user(name)
        self.add_certificate(name, certificate)
        self.add_principal(name, principal, ssh)
        self.add_known_host(ssh)

    @step("Add User to Proxy")
    def add_user(self, name):
        return self.docker_execute(f"useradd -m -p '*' {name}")

    @step("Add Certificate to User")
    def add_certificate(self, name, certificate):
        self.docker_execute(f"mkdir /home/{name}/.ssh")
        self.docker_execute(f"chown {name}:{name} /home/{name}/.ssh")
        for key, value in certificate.items():
            source = tempfile.mkstemp()[1]
            with open(source, "w") as f:
                f.write(value)
            target = f"/home/{name}/.ssh/{key}"
            self.execute(f"docker cp {source} ssh:{target}")
            self.docker_execute(f"chown {name}:{name} {target}")
            os.remove(source)

    @step("Add Principal to User")
    def add_principal(self, name, principal, ssh):
        cd_command = "cd frappe-bench; exec bash --login"
        force_command = f"ssh -A frappe@{ssh['ip']} -p {ssh['port']} -t '{cd_command}'"
        principal_line = f'pty,agent-forwarding,no-port-forwarding,no-x11-forwarding,no-user-rc,command="{force_command}" {principal}'
        source = tempfile.mkstemp()[1]
        with open(source, "w") as f:
            f.write(principal_line)
        target = f"/etc/ssh/principals/{name}"
        self.execute(f"docker cp {source} ssh:{target}")
        self.docker_execute(f"chown {name}:{name} {target}")
        os.remove(source)

    @step("Add Container Host Key to known_hosts")
    def add_known_host(self, ssh):
        # ssh-keyscan the bench container's sshd, dedupe, append to proxy's ssh_known_hosts.
        # Without this, the forced-command `ssh frappe@<ip>:<port>` aborts with
        # "Host key verification failed" right after the user authenticates.
        host_pattern = f"[{ssh['ip']}]:{ssh['port']}"
        # ssh-keygen -R is idempotent: removes any existing entry for this host pattern.
        # Then ssh-keyscan appends the live hostkey. The two steps together = atomic refresh.
        self.docker_execute(f"ssh-keygen -R '{host_pattern}' -f /etc/ssh/ssh_known_hosts")
        self.docker_execute(
            f"sh -c 'ssh-keyscan -p {ssh['port']} -t rsa {ssh['ip']} >> /etc/ssh/ssh_known_hosts'"
        )

    @job("Remove User from Proxy")
    def remove_user_job(self, name, ssh=None):
        self.remove_user(name)
        self.remove_principal(name)
        if ssh:
            self.remove_known_host(ssh)

    @step("Remove User from Proxy")
    def remove_user(self, name):
        return self.docker_execute(f"userdel -f -r {name}")

    @step("Remove Principal from User")
    def remove_principal(self, name):
        command = f"rm /etc/ssh/principals/{name}"
        return self.docker_execute(command)

    @step("Remove Container Host Key from known_hosts")
    def remove_known_host(self, ssh):
        host_pattern = f"[{ssh['ip']}]:{ssh['port']}"
        return self.docker_execute(
            f"ssh-keygen -R '{host_pattern}' -f /etc/ssh/ssh_known_hosts"
        )
