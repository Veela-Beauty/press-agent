from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import time
from datetime import datetime
from subprocess import Popen
from typing import TYPE_CHECKING

import docker

from agent.base import Base
from agent.exceptions import (
    BuildConcurrencyLimitException,
    LowMemoryException,
    RegistryDownException,
)
from agent.job import Job, Step, job, step
from agent.utils import is_registry_healthy

DEFAULT_BUILD_MIN_MEMORY_MB = 5120  # 5 GB free required before starting a build
DEFAULT_MAX_CONCURRENT_BUILDS = 2
BUILD_SLOTS_DIR = "/tmp/agent-build-slots"

if TYPE_CHECKING:
    from typing import Literal

    OutputKey = Literal["build", "push"]
    Output = dict[OutputKey, list[str]]


class ImageBuilder(Base):
    output: Output

    def __init__(
        self,
        filename: str,
        image_repository: str,
        image_tag: str,
        no_cache: bool,
        no_push: bool,
        registry: dict,
        platform: str,
        build_token: str,
    ) -> None:
        super().__init__()

        # Image push params
        self.image_repository = image_repository
        self.image_tag = image_tag
        self.registry = registry
        self.platform = platform

        # Build context, params
        self.filename = filename
        self.filepath = os.path.join(
            get_image_build_context_directory(),
            self.filename,
        )
        self.no_cache = no_cache
        self.no_push = no_push
        self.last_published = datetime.now()
        self.build_failed = False
        self.build_token = build_token
        self.secret_path = None

        cwd = os.getcwd()
        self.config_file = os.path.join(cwd, "config.json")

        # Lines from build and push are sent to press for processing
        # and updating the respective Deploy Candidate
        self.output = {
            "build": [],
            "push": [],
        }
        self.push_output_lines = []

        self.job = None
        self.step = None

    @property
    def job_record(self):
        if self.job is None:
            self.job = Job()
        return self.job

    @property
    def step_record(self):
        if self.step is None:
            self.step = Step()
        return self.step

    @step_record.setter
    def step_record(self, value):
        self.step = value

    @job("Run Remote Builder")
    def run_remote_builder(self):
        slot_fd = None
        try:
            self._check_memory_available()
            slot_fd = self._acquire_build_slot()
            return self._build_and_push()
        finally:
            self._release_build_slot(slot_fd)
            self._cleanup_context()

    @step("Check Memory Available")
    def _check_memory_available(self):
        """Refuse to start build if free RAM is below the configured threshold.

        Prevents OOM cascades when Press lands many parallel deploys on a
        host already running benches. The job fails fast (no docker layers
        attempted, no context wasted); Press auto-retries on next poll, so
        the build runs as soon as memory frees.
        """
        import psutil

        threshold_mb = self._get_build_memory_threshold_mb()
        available_mb = psutil.virtual_memory().available // (1024 * 1024)
        if available_mb < threshold_mb:
            raise LowMemoryException(
                available_mb=available_mb,
                required_mb=threshold_mb,
            )
        return {"available_mb": available_mb, "threshold_mb": threshold_mb}

    @step("Acquire Build Slot")
    def _acquire_build_slot(self):
        """Try to grab one of N concurrent build slots via non-blocking flock.

        Slot files live under BUILD_SLOTS_DIR (slot_0, slot_1, ...). Each
        running build holds an exclusive flock on one slot file. When the
        build process exits — clean or crash — the kernel releases the
        lock automatically. No PID files to clean up, no stale-lock bug.

        If all slots are held, raises BuildConcurrencyLimitException; Press
        marks the job Failure and retries on next poll.
        """
        max_builds = self._get_max_concurrent_builds()
        os.makedirs(BUILD_SLOTS_DIR, exist_ok=True)
        for slot_index in range(max_builds):
            slot_path = os.path.join(BUILD_SLOTS_DIR, f"slot_{slot_index}")
            fd = os.open(slot_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}\n".encode())
            os.fsync(fd)
            return fd
        raise BuildConcurrencyLimitException(max_concurrent_builds=max_builds)

    def _release_build_slot(self, slot_fd):
        if slot_fd is None:
            return
        try:
            fcntl.flock(slot_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(slot_fd)
        except OSError:
            pass

    def _get_build_memory_threshold_mb(self) -> int:
        # Read once per build from server config (no agent restart needed
        # when an operator tunes the threshold via config.json).
        try:
            with open(self.config_file) as f:
                cfg = json.load(f)
            return int(cfg.get("build_min_memory_mb") or DEFAULT_BUILD_MIN_MEMORY_MB)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return DEFAULT_BUILD_MIN_MEMORY_MB

    def _get_max_concurrent_builds(self) -> int:
        try:
            with open(self.config_file) as f:
                cfg = json.load(f)
            return int(cfg.get("max_concurrent_builds") or DEFAULT_MAX_CONCURRENT_BUILDS)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return DEFAULT_MAX_CONCURRENT_BUILDS

    def _build_and_push(self):
        self._build_image()
        if not self.build_failed and not self.no_push:
            self._push_docker_image()
        return self.data

    @step("Build Image")
    def _build_image(self):
        # Note: build command and environment are different from when
        # build runs on the press server.
        command = self._get_build_command()
        environment = self._get_build_environment()
        result = self._run(
            command=command,
            environment=environment,
            input_filepath=self.filepath,
        )
        self.output["build"] = []
        self._publish_docker_build_output(result)
        return {"output": self.output["build"]}

    def _get_build_command(self) -> str:
        command = f"docker buildx build --platform {self.platform}"

        if self.build_token:
            with tempfile.NamedTemporaryFile(
                delete=False,
                mode="w",
                prefix="buildtoken-secret-",
            ) as tmp:
                tmp.write(self.build_token)
                os.chmod(tmp.name, 0o600)
                self.secret_path = tmp.name

            command = f"{command} --secret id=build_token,src={self.secret_path}"

        command = f"{command} -t {self._get_image_name()}"

        if self.no_cache:
            command = f"{command} --no-cache"

        return f"{command} - "

    def _get_build_environment(self) -> dict:
        environment = os.environ.copy()
        environment.update(
            {
                "DOCKER_BUILDKIT": "1",
                "BUILDKIT_PROGRESS": "plain",
                "PROGRESS_NO_TRUNC": "1",
            }
        )
        return environment

    def _publish_docker_build_output(self, result):
        for line in result:
            self.output["build"].append(line)
            self._publish_throttled_output(False)
        self._publish_throttled_output(True)

    def _wait_for_registry_recovery(self):
        """Wait for registry to recover after restart"""
        time.sleep(60)

    @step("Push Docker Image")
    def _push_docker_image(self):
        max_retries = 3
        environment = os.environ.copy()
        client = docker.from_env(environment=environment, timeout=5 * 60)

        for attempt in range(max_retries):
            self.output["push"].append({"id": "Retry", "output": "", "status": f"Success {attempt}"})
            try:
                if not is_registry_healthy(
                    self.registry["url"], self.registry["username"], self.registry["password"]
                ):
                    raise RegistryDownException("Registry is currently down")

                self._push_image(client)

                if not is_registry_healthy(
                    self.registry["url"], self.registry["username"], self.registry["password"]
                ):
                    raise RegistryDownException("Registry became unhealthy after push")

                return self.output["push"]

            except RegistryDownException as e:
                if attempt == max_retries - 1:
                    self._publish_throttled_output(True)
                    raise Exception("Failed to push image after multiple attempts") from e

                self._wait_for_registry_recovery()

            except Exception:
                self._publish_throttled_output(True)
                raise

        return None

    def _push_image(self, client):
        auth_config = {
            "username": self.registry["username"],
            "password": self.registry["password"],
            "serveraddress": self.registry["url"],
        }
        for line in client.images.push(
            self.image_repository,
            self.image_tag,
            stream=True,
            decode=True,
            auth_config=auth_config,
        ):
            self.output["push"].append(line)
            self._publish_throttled_output(False)

    def _publish_throttled_output(self, flush: bool):
        if flush:
            self.publish_data(self.output)
            return

        now = datetime.now()
        if (now - self.last_published).total_seconds() <= 1:
            return

        self.last_published = now
        self.publish_data(self.output)

    def _get_image_name(self):
        return f"{self.image_repository}:{self.image_tag}"

    def _run(
        self,
        command: str,
        environment: dict,
        input_filepath: str,
    ):
        with open(input_filepath, "rb") as input_file:
            process = Popen(
                shlex.split(command),
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=environment,
                universal_newlines=True,
            )

        yield from process.stdout

        process.stdout.close()
        input_file.close()

        return_code = process.wait()
        self._publish_throttled_output(True)

        self.build_failed = return_code != 0
        self.data.update({"build_failed": self.build_failed})

        if self.secret_path:
            os.remove(self.secret_path)

    @step("Cleanup Context")
    def _cleanup_context(self):
        if not os.path.exists(self.filepath):
            return {"cleanup": False}

        os.remove(self.filepath)
        return {"cleanup": True}


def get_image_build_context_directory():
    path = os.path.join(os.getcwd(), "build_context")
    if not os.path.exists(path):
        os.makedirs(path)
    return path
