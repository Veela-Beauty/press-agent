from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestBuildMemoryGuard(unittest.TestCase):
    """Regression test for the OOM cascade incident on 2026-05-24.

    Background: Press landed ~10 parallel deploys on press-f1 within the
    same second; each spawned a vite build worth ~2 GB. The 15 GB box ran
    out of RAM, swap filled, load shot to 270, SSH stopped responding.

    The guard refuses to start a build when available memory is below a
    configurable threshold (default 3 GB). Press treats the resulting
    Failure as transient and retries on next agent poll.
    """

    def _make_builder(self, config_overrides=None):
        # Bypass ImageBuilder.__init__ (it imports docker and tries to read
        # config) so we can unit-test the guard in isolation.
        from agent.builder import ImageBuilder

        builder = ImageBuilder.__new__(ImageBuilder)

        config = {"name": "test-server"}
        if config_overrides:
            config.update(config_overrides)

        cfg_dir = tempfile.mkdtemp()
        cfg_path = os.path.join(cfg_dir, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(config, f)
        builder.config_file = cfg_path

        builder.job = None
        builder.step = None
        return builder

    def test_default_threshold_is_5gb(self):
        from agent.builder import DEFAULT_BUILD_MIN_MEMORY_MB

        assert DEFAULT_BUILD_MIN_MEMORY_MB == 5120, (
            f"default threshold must stay 5 GB to match N=2 cap; "
            f"got {DEFAULT_BUILD_MIN_MEMORY_MB} MB"
        )

    def test_guard_allows_build_when_memory_sufficient(self):
        from agent.builder import ImageBuilder

        builder = self._make_builder()

        # Simulate 8 GB available (well above 5 GB default)
        fake_mem = MagicMock(available=8 * 1024 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            result = ImageBuilder._check_memory_available.__wrapped__(builder)

        assert result["available_mb"] == 8192
        assert result["threshold_mb"] == 5120

    def test_guard_refuses_build_when_memory_low(self):
        from agent.builder import ImageBuilder
        from agent.exceptions import LowMemoryException

        builder = self._make_builder()

        # Simulate 500 MB available (well below 5 GB default)
        fake_mem = MagicMock(available=500 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            with self.assertRaises(LowMemoryException) as ctx:
                ImageBuilder._check_memory_available.__wrapped__(builder)

        assert ctx.exception.available_mb == 500
        assert ctx.exception.required_mb == 5120
        assert "Refusing to start build" in str(ctx.exception)
        assert "Will retry on next agent poll" in str(ctx.exception)

    def test_guard_threshold_overridable_via_config(self):
        from agent.builder import ImageBuilder
        from agent.exceptions import LowMemoryException

        # Operator wants to be stricter: require 8 GB
        builder = self._make_builder({"build_min_memory_mb": 8192})

        # 6 GB available — would pass default (5 GB) but should fail at 8 GB
        fake_mem = MagicMock(available=6 * 1024 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            with self.assertRaises(LowMemoryException) as ctx:
                ImageBuilder._check_memory_available.__wrapped__(builder)

        assert ctx.exception.required_mb == 8192

    def test_guard_falls_back_to_default_on_bad_config(self):
        from agent.builder import ImageBuilder

        builder = self._make_builder()
        # Corrupt the config file
        with open(builder.config_file, "w") as f:
            f.write("not json at all")

        # 8 GB available — must still allow since default (5 GB) takes over
        fake_mem = MagicMock(available=8 * 1024 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            result = ImageBuilder._check_memory_available.__wrapped__(builder)

        assert result["threshold_mb"] == 5120


class TestBuildConcurrencyCap(unittest.TestCase):
    """Regression for the OOM cascade: cap parallel builds per server.

    Without this cap, 10 simultaneous Deploy Candidate Builds spawned 10
    parallel vite processes (~2 GB each) and OOMed the box even if the
    memory guard would have refused later ones — because each build
    decided to start INDEPENDENTLY before any of them had measurable load.
    """

    def setUp(self):
        # Use a unique slot dir per test to avoid cross-test interference.
        import tempfile
        from agent import builder

        self._test_slots_dir = tempfile.mkdtemp(prefix="agent-test-slots-")
        self._original_slots_dir = builder.BUILD_SLOTS_DIR
        builder.BUILD_SLOTS_DIR = self._test_slots_dir

    def tearDown(self):
        import shutil
        from agent import builder

        builder.BUILD_SLOTS_DIR = self._original_slots_dir
        shutil.rmtree(self._test_slots_dir, ignore_errors=True)

    def _make_builder(self, config_overrides=None):
        from agent.builder import ImageBuilder

        builder = ImageBuilder.__new__(ImageBuilder)
        config = {"name": "test-server"}
        if config_overrides:
            config.update(config_overrides)
        cfg_dir = tempfile.mkdtemp()
        cfg_path = os.path.join(cfg_dir, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(config, f)
        builder.config_file = cfg_path
        builder.job = None
        builder.step = None
        return builder

    def test_default_max_concurrent_builds_is_2(self):
        from agent.builder import DEFAULT_MAX_CONCURRENT_BUILDS

        assert DEFAULT_MAX_CONCURRENT_BUILDS == 2, (
            f"per session decision: N=2 cap; got {DEFAULT_MAX_CONCURRENT_BUILDS}"
        )

    def test_first_build_acquires_slot(self):
        from agent.builder import ImageBuilder

        builder = self._make_builder()
        fd = ImageBuilder._acquire_build_slot.__wrapped__(builder)
        assert fd is not None
        ImageBuilder._release_build_slot(builder, fd)

    def test_third_build_refused_when_2_already_running(self):
        from agent.builder import ImageBuilder
        from agent.exceptions import BuildConcurrencyLimitException

        b1 = self._make_builder()
        b2 = self._make_builder()
        b3 = self._make_builder()

        fd1 = ImageBuilder._acquire_build_slot.__wrapped__(b1)
        fd2 = ImageBuilder._acquire_build_slot.__wrapped__(b2)

        with self.assertRaises(BuildConcurrencyLimitException) as ctx:
            ImageBuilder._acquire_build_slot.__wrapped__(b3)
        assert ctx.exception.max_concurrent_builds == 2
        assert "2 build(s) already running" in str(ctx.exception)

        # Release one slot — third build should now succeed
        ImageBuilder._release_build_slot(b1, fd1)
        fd3 = ImageBuilder._acquire_build_slot.__wrapped__(b3)
        assert fd3 is not None

        ImageBuilder._release_build_slot(b2, fd2)
        ImageBuilder._release_build_slot(b3, fd3)

    def test_cap_overridable_via_config(self):
        from agent.builder import ImageBuilder
        from agent.exceptions import BuildConcurrencyLimitException

        b1 = self._make_builder({"max_concurrent_builds": 1})
        b2 = self._make_builder({"max_concurrent_builds": 1})

        fd1 = ImageBuilder._acquire_build_slot.__wrapped__(b1)
        with self.assertRaises(BuildConcurrencyLimitException) as ctx:
            ImageBuilder._acquire_build_slot.__wrapped__(b2)
        assert ctx.exception.max_concurrent_builds == 1
        ImageBuilder._release_build_slot(b1, fd1)

    def test_release_with_none_fd_is_safe(self):
        # When the cap check raises BEFORE acquiring (e.g., LowMemory fires
        # first), the finally-clause calls _release_build_slot(None).
        from agent.builder import ImageBuilder

        b = self._make_builder()
        # Should not raise
        ImageBuilder._release_build_slot(b, None)


if __name__ == "__main__":
    unittest.main()
