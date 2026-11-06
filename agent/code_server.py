"""Start code-server inside a bench container without the first-launch races.

Race 1: Press queues "Update Bench Configuration" (adds [program:code-server]
to supervisor.conf) and "Setup Code Server" together; on another worker the
setup can reach `supervisorctl start` first and fail with exit 2.
Race 2: code-server writes ~/.config/code-server/config.yaml a moment AFTER
supervisor reports it running, so an immediate sed on that file fails.

The password reaches the container on stdin, never on the command line, so it
is not stored in the job step's command or traceback.
"""

from __future__ import annotations

import re
import time

CONFIG = "/home/frappe/.config/code-server/config.yaml"
PROGRAM_WAIT_SECONDS = 300  # a bench config update took ~3 min on u5
CONFIG_WAIT_SECONDS = 60
POLL_SECONDS = 5

PROGRAM_LOADED = "supervisorctl avail | grep -qw code-server"
CONFIG_EXISTS = f"test -f {CONFIG}"
SET_PASSWORD = f"sh -c 'read -r pw && sed -i \"s/^password:.*/password: $pw/\" {CONFIG}'"


def start_code_server(bench, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9]+", password or ""):
        raise ValueError("code-server password must be alphanumeric")

    _wait_for(bench, PROGRAM_LOADED, PROGRAM_WAIT_SECONDS, "code-server program in supervisor")
    if not _succeeds(bench, CONFIG_EXISTS):
        bench.docker_execute("supervisorctl start code-server:", non_zero_throw=False)
        _wait_for(bench, CONFIG_EXISTS, CONFIG_WAIT_SECONDS, f"{CONFIG} (see logs/code-server.log)")

    bench.docker_execute(SET_PASSWORD, input=f"{password}\n")
    bench.docker_execute("supervisorctl restart code-server:")


def _succeeds(bench, command: str) -> bool:
    return bench.docker_execute(command, non_zero_throw=False)["returncode"] == 0


def _wait_for(bench, command: str, timeout: int, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not _succeeds(bench, command):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Waited {timeout}s for {what} in bench {bench.name}")
        time.sleep(POLL_SECONDS)
