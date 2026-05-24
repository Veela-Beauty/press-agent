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

    def test_default_threshold_is_3gb(self):
        from agent.builder import DEFAULT_BUILD_MIN_MEMORY_MB

        assert DEFAULT_BUILD_MIN_MEMORY_MB == 3072, (
            f"default threshold must stay 3 GB; got {DEFAULT_BUILD_MIN_MEMORY_MB} MB"
        )

    def test_guard_allows_build_when_memory_sufficient(self):
        from agent.builder import ImageBuilder

        builder = self._make_builder()

        # Simulate 8 GB available (well above 3 GB default)
        fake_mem = MagicMock(available=8 * 1024 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            result = ImageBuilder._check_memory_available.__wrapped__(builder)

        assert result["available_mb"] == 8192
        assert result["threshold_mb"] == 3072

    def test_guard_refuses_build_when_memory_low(self):
        from agent.builder import ImageBuilder
        from agent.exceptions import LowMemoryException

        builder = self._make_builder()

        # Simulate 500 MB available (well below 3 GB default)
        fake_mem = MagicMock(available=500 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            with self.assertRaises(LowMemoryException) as ctx:
                ImageBuilder._check_memory_available.__wrapped__(builder)

        assert ctx.exception.available_mb == 500
        assert ctx.exception.required_mb == 3072
        assert "Refusing to start build" in str(ctx.exception)
        assert "Will retry on next agent poll" in str(ctx.exception)

    def test_guard_threshold_overridable_via_config(self):
        from agent.builder import ImageBuilder
        from agent.exceptions import LowMemoryException

        # Operator wants to be stricter: require 6 GB
        builder = self._make_builder({"build_min_memory_mb": 6144})

        # 4 GB available — would pass default (3 GB) but should fail at 6 GB
        fake_mem = MagicMock(available=4 * 1024 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            with self.assertRaises(LowMemoryException) as ctx:
                ImageBuilder._check_memory_available.__wrapped__(builder)

        assert ctx.exception.required_mb == 6144

    def test_guard_falls_back_to_default_on_bad_config(self):
        from agent.builder import ImageBuilder

        builder = self._make_builder()
        # Corrupt the config file
        with open(builder.config_file, "w") as f:
            f.write("not json at all")

        # 8 GB available — must still allow since default (3 GB) takes over
        fake_mem = MagicMock(available=8 * 1024 * 1024 * 1024)
        with patch("psutil.virtual_memory", return_value=fake_mem):
            result = ImageBuilder._check_memory_available.__wrapped__(builder)

        assert result["threshold_mb"] == 3072


if __name__ == "__main__":
    unittest.main()
