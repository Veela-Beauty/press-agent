"""
TDD tests for Site restore extensions:
  - restore_job accepts skip_tables param
  - filtered restore creates a filtered SQL file and passes it to bench
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call


class TestFilteredRestore(unittest.TestCase):
    """
    Tests for filter_and_restore_site() — the new step that
    filters noise tables out of a .sql.gz before importing.
    """

    def setUp(self):
        """Create a minimal site environment."""
        self.test_dir = tempfile.mkdtemp()
        self.bench_dir = os.path.join(self.test_dir, "bench")
        self.sites_dir = os.path.join(self.bench_dir, "sites")
        self.site_name = "test.example.com"
        self.site_dir = os.path.join(self.sites_dir, self.site_name)
        os.makedirs(self.site_dir)

        # Minimal site_config.json
        with open(os.path.join(self.site_dir, "site_config.json"), "w") as f:
            json.dump({"db_name": "testdb", "db_password": "testpw"}, f)

        # Minimal bench config
        os.makedirs(os.path.join(self.bench_dir, "apps"))
        with open(os.path.join(self.bench_dir, "config.json"), "w") as f:
            json.dump({"docker_image": "fake"}, f)
        with open(os.path.join(self.sites_dir, "common_site_config.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(self.sites_dir, "apps.txt"), "w") as f:
            f.write("frappe\n")
        os.makedirs(os.path.join(self.sites_dir, "assets"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_sql_gz(self, content: str, path: str) -> str:
        with gzip.open(path, "wt", encoding="utf-8") as gz:
            gz.write(content)
        return path

    def test_filter_and_restore_creates_filtered_file(self):
        """
        filter_and_restore_site() must create a filtered .sql.gz
        that excludes INSERT statements for skip_tables.
        """
        from agent.site import Site

        # Create a fake SQL backup with both noise and custom data
        sql = (
            "-- Dumping data for table `tabActivity Log`\n"
            "LOCK TABLES `tabActivity Log` WRITE;\n"
            "INSERT INTO `tabActivity Log` VALUES (1),(2),(3);\n"
            "UNLOCK TABLES;\n"
            "-- Dumping data for table `tabCustom Field`\n"
            "LOCK TABLES `tabCustom Field` WRITE;\n"
            "INSERT INTO `tabCustom Field` VALUES (10),(20);\n"
            "UNLOCK TABLES;\n"
        )
        db_file = os.path.join(self.test_dir, "backup.sql.gz")
        self._make_sql_gz(sql, db_file)

        bench = MagicMock()
        bench.sites_directory = self.sites_dir
        bench.host = "localhost"
        bench.db_port = 3306
        site = Site.__new__(Site)
        site.name = self.site_name
        site.bench = bench
        site.directory = self.site_dir
        site.database = "testdb"
        site.user = "testdb"
        site.password = "testpw"
        site.host = "localhost"
        site.db_port = 3306

        filtered_path = site._create_filtered_backup(
            db_file, skip_tables=["tabActivity Log"]
        )

        try:
            self.assertTrue(os.path.exists(filtered_path))
            # Read filtered content
            with gzip.open(filtered_path, "rt") as f:
                content = f.read()
            self.assertNotIn("INSERT INTO `tabActivity Log`", content)
            self.assertIn("INSERT INTO `tabCustom Field`", content)
        finally:
            if os.path.exists(filtered_path):
                os.unlink(filtered_path)

    def test_filter_and_restore_passes_filtered_file_to_bench(self):
        """
        When skip_tables is provided, restore_site() must use the
        filtered backup file, not the original.
        """
        from agent.site import Site

        sql = (
            "INSERT INTO `tabActivity Log` VALUES (1);\n"
            "INSERT INTO `tabCustom Field` VALUES (10);\n"
        )
        db_file = os.path.join(self.test_dir, "backup.sql.gz")
        self._make_sql_gz(sql, db_file)

        bench = MagicMock()
        bench.sites_directory = self.sites_dir
        bench.host = "localhost"
        bench.db_port = 3306
        bench.create_mariadb_user.return_value = ("db", "tmpuser", "tmppw")
        bench.docker_execute.return_value = {"output": "ok"}

        site = Site.__new__(Site)
        site.name = self.site_name
        site.bench = bench
        site.directory = self.site_dir
        site.database = "testdb"
        site.user = "testdb"
        site.password = "testpw"
        site.host = "localhost"
        site.db_port = 3306

        site.restore_site(
            mariadb_root_password="root",
            admin_password="admin",
            database_file=db_file,
            public_file=None,
            private_file=None,
            skip_tables=["tabActivity Log"],
        )

        # bench_execute must have been called — the argument should NOT be
        # the original db_file path (it must use the filtered path instead)
        bench.docker_execute.assert_called_once()
        cmd = bench.docker_execute.call_args[0][0]
        # The filtered file is named with a -filtered suffix
        self.assertIn("-filtered.sql.gz", cmd)

    def test_restore_site_no_skip_tables_unchanged_behavior(self):
        """
        When skip_tables is empty/None, restore_site() behaves exactly
        as before — no filtering, original file passed to bench.
        """
        from agent.site import Site

        db_file = os.path.join(self.test_dir, "backup.sql.gz")
        with gzip.open(db_file, "wt") as f:
            f.write("INSERT INTO `tabCustom Field` VALUES (1);\n")

        bench = MagicMock()
        bench.sites_directory = self.sites_dir
        bench.host = "localhost"
        bench.db_port = 3306
        bench.create_mariadb_user.return_value = ("db", "tmpuser", "tmppw")
        bench.docker_execute.return_value = {"output": "ok"}

        site = Site.__new__(Site)
        site.name = self.site_name
        site.bench = bench
        site.directory = self.site_dir
        site.database = "testdb"
        site.user = "testdb"
        site.password = "testpw"
        site.host = "localhost"
        site.db_port = 3306

        # Call with no skip_tables — should work exactly as before
        site.restore_site(
            mariadb_root_password="root",
            admin_password="admin",
            database_file=db_file,
            public_file=None,
            private_file=None,
        )

        bench.docker_execute.assert_called_once()
        cmd = bench.docker_execute.call_args[0][0]
        # Must NOT contain -filtered
        self.assertNotIn("-filtered.sql.gz", cmd)


if __name__ == "__main__":
    unittest.main()
