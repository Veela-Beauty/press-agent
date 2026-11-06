from __future__ import annotations

import itertools
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import code_server
from agent.code_server import CONFIG_EXISTS, PROGRAM_LOADED, SET_PASSWORD, start_code_server

PASSWORD = "eb5b90e34031be9482b56478b436f42dfffebec3"
START = "supervisorctl start code-server:"
RESTART = "supervisorctl restart code-server:"


class FakeBench:
    """Container whose supervisor program and config.yaml appear late."""

    name = "bench-test"

    def __init__(self, program_after=0, config_after=0, config_exists=False):
        self.program_after = program_after
        self.config_after = config_after
        self.config_exists = config_exists
        self.started = False
        self.calls = []

    def docker_execute(self, command, input=None, non_zero_throw=True, **kwargs):
        self.calls.append((command, input))
        ok = True
        if command == PROGRAM_LOADED:
            ok = self.program_after <= 0
            self.program_after -= 1
        elif command == CONFIG_EXISTS:
            if self.started and not self.config_exists:
                self.config_exists = self.config_after <= 0
                self.config_after -= 1
            ok = self.config_exists
        elif command == START:
            self.started = True
        elif command == SET_PASSWORD and not self.config_exists:
            ok = False  # real sed exits 2: config.yaml not written yet
        if non_zero_throw and not ok:
            raise RuntimeError(f"exit 2: {command}")
        return {"returncode": 0 if ok else 1}

    def commands(self):
        return [command for command, _ in self.calls]


@patch.object(code_server.time, "sleep", lambda _: None)
class TestStartCodeServer(unittest.TestCase):
    def test_first_launch_waits_for_program_then_config(self):
        bench = FakeBench(program_after=2, config_after=1)
        start_code_server(bench, PASSWORD)
        commands = bench.commands()
        self.assertEqual(commands.count(PROGRAM_LOADED), 3)
        self.assertLess(commands.index(START), commands.index(SET_PASSWORD))
        self.assertLess(commands.index(SET_PASSWORD), commands.index(RESTART))
        self.assertEqual(commands[-1], RESTART)

    def test_password_only_on_stdin(self):
        bench = FakeBench()
        start_code_server(bench, PASSWORD)
        self.assertFalse(any(PASSWORD in command for command in bench.commands()))
        self.assertIn((SET_PASSWORD, f"{PASSWORD}\n"), bench.calls)

    def test_existing_config_skips_first_start(self):
        bench = FakeBench(config_exists=True)
        start_code_server(bench, PASSWORD)
        self.assertNotIn(START, bench.commands())
        self.assertEqual(bench.commands()[-2:], [SET_PASSWORD, RESTART])

    def test_times_out_when_program_never_loads(self):
        bench = FakeBench(program_after=10**6)
        clock = itertools.count(0, code_server.POLL_SECONDS)
        with patch.object(code_server.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(TimeoutError):
                start_code_server(bench, PASSWORD)
        self.assertNotIn(SET_PASSWORD, bench.commands())

    def test_rejects_non_alphanumeric_password(self):
        bench = FakeBench()
        with self.assertRaises(ValueError):
            start_code_server(bench, "abc'; rm -rf / #")
        self.assertEqual(bench.calls, [])


class TestSetPasswordShell(unittest.TestCase):
    """Runs the real SET_PASSWORD string through sh, parsed by a host shell
    exactly as it is after `docker exec ... -i <container>`."""

    def test_sets_password_line_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("bind-addr: 127.0.0.1:8080\nauth: password\npassword: old\ncert: false\n")
            command = "env " + SET_PASSWORD.replace(code_server.CONFIG, str(config))
            result = subprocess.run(
                command, shell=True, input=f"{PASSWORD}\n", text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                config.read_text().splitlines(),
                ["bind-addr: 127.0.0.1:8080", "auth: password", f"password: {PASSWORD}", "cert: false"],
            )


if __name__ == "__main__":
    unittest.main()
