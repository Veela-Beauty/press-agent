"""
TDD tests for backup_analyzer module.
RED phase: all tests MUST fail before implementation.
"""
from __future__ import annotations

import gzip
import io
import os
import tempfile
import unittest


class TestAnalyzeBackup(unittest.TestCase):
    """Tests for analyze_backup() - table size scanning from .sql.gz files."""

    def _make_sql_gz(self, content: str) -> str:
        """Write content to a temp .sql.gz file, return path."""
        f = tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False)
        with gzip.open(f.name, "wt", encoding="utf-8") as gz:
            gz.write(content)
        return f.name

    def test_analyze_returns_table_names(self):
        """analyze_backup returns a dict with all table names found in dump."""
        from agent.backup_analyzer import analyze_backup

        sql = (
            "-- Dumping data for table `tabCustom Field`\n"
            "LOCK TABLES `tabCustom Field` WRITE;\n"
            "INSERT INTO `tabCustom Field` VALUES (1),(2),(3);\n"
            "UNLOCK TABLES;\n"
            "-- Dumping data for table `tabActivity Log`\n"
            "LOCK TABLES `tabActivity Log` WRITE;\n"
            "INSERT INTO `tabActivity Log` VALUES (1),(2);\n"
            "UNLOCK TABLES;\n"
        )
        path = self._make_sql_gz(sql)
        try:
            result = analyze_backup(path)
            self.assertIn("tabCustom Field", result["tables"])
            self.assertIn("tabActivity Log", result["tables"])
        finally:
            os.unlink(path)

    def test_analyze_counts_rows_per_table(self):
        """analyze_backup counts the number of VALUE tuples per table."""
        from agent.backup_analyzer import analyze_backup

        sql = (
            "-- Dumping data for table `tabError Log`\n"
            "LOCK TABLES `tabError Log` WRITE;\n"
            "INSERT INTO `tabError Log` VALUES (1,'a'),(2,'b'),(3,'c');\n"
            "UNLOCK TABLES;\n"
        )
        path = self._make_sql_gz(sql)
        try:
            result = analyze_backup(path)
            self.assertEqual(result["tables"]["tabError Log"]["rows"], 3)
        finally:
            os.unlink(path)

    def test_analyze_flags_noise_tables(self):
        """analyze_backup flags known noise tables as noise=True."""
        from agent.backup_analyzer import analyze_backup

        sql = (
            "-- Dumping data for table `tabActivity Log`\n"
            "LOCK TABLES `tabActivity Log` WRITE;\n"
            "INSERT INTO `tabActivity Log` VALUES (1);\n"
            "UNLOCK TABLES;\n"
            "-- Dumping data for table `tabCustom Field`\n"
            "LOCK TABLES `tabCustom Field` WRITE;\n"
            "INSERT INTO `tabCustom Field` VALUES (1);\n"
            "UNLOCK TABLES;\n"
        )
        path = self._make_sql_gz(sql)
        try:
            result = analyze_backup(path)
            self.assertTrue(result["tables"]["tabActivity Log"]["noise"])
            self.assertFalse(result["tables"]["tabCustom Field"]["noise"])
        finally:
            os.unlink(path)

    def test_analyze_returns_noise_summary(self):
        """analyze_backup returns noise_tables list and noise_row_count."""
        from agent.backup_analyzer import analyze_backup

        sql = (
            "-- Dumping data for table `tabError Log`\n"
            "LOCK TABLES `tabError Log` WRITE;\n"
            "INSERT INTO `tabError Log` VALUES (1),(2),(3),(4),(5);\n"
            "UNLOCK TABLES;\n"
            "-- Dumping data for table `tabCustom Field`\n"
            "LOCK TABLES `tabCustom Field` WRITE;\n"
            "INSERT INTO `tabCustom Field` VALUES (1),(2);\n"
            "UNLOCK TABLES;\n"
        )
        path = self._make_sql_gz(sql)
        try:
            result = analyze_backup(path)
            self.assertIn("tabError Log", result["noise_tables"])
            self.assertNotIn("tabCustom Field", result["noise_tables"])
            self.assertEqual(result["noise_row_count"], 5)
            self.assertEqual(result["total_row_count"], 7)
        finally:
            os.unlink(path)

    def test_analyze_handles_empty_table(self):
        """analyze_backup handles tables with no INSERT (empty tables)."""
        from agent.backup_analyzer import analyze_backup

        sql = (
            "-- Dumping data for table `tabEmpty Table`\n"
            "LOCK TABLES `tabEmpty Table` WRITE;\n"
            "/*!40000 ALTER TABLE `tabEmpty Table` DISABLE KEYS */;\n"
            "/*!40000 ALTER TABLE `tabEmpty Table` ENABLE KEYS */;\n"
            "UNLOCK TABLES;\n"
        )
        path = self._make_sql_gz(sql)
        try:
            result = analyze_backup(path)
            self.assertIn("tabEmpty Table", result["tables"])
            self.assertEqual(result["tables"]["tabEmpty Table"]["rows"], 0)
        finally:
            os.unlink(path)


class TestFilterSqlStream(unittest.TestCase):
    """Tests for filter_sql_stream() - skip INSERT statements for specified tables."""

    def test_filter_removes_inserts_for_skip_table(self):
        """filter_sql_stream removes INSERT lines for tables in skip_tables."""
        from agent.backup_analyzer import filter_sql_stream

        sql = (
            "CREATE TABLE `tabActivity Log` (id int);\n"
            "LOCK TABLES `tabActivity Log` WRITE;\n"
            "INSERT INTO `tabActivity Log` VALUES (1),(2);\n"
            "UNLOCK TABLES;\n"
            "CREATE TABLE `tabCustom Field` (id int);\n"
            "LOCK TABLES `tabCustom Field` WRITE;\n"
            "INSERT INTO `tabCustom Field` VALUES (10),(20);\n"
            "UNLOCK TABLES;\n"
        )
        inp = io.StringIO(sql)
        out = io.StringIO()
        filter_sql_stream(inp, skip_tables=["tabActivity Log"], output=out)
        result = out.getvalue()

        self.assertNotIn("INSERT INTO `tabActivity Log`", result)
        self.assertIn("INSERT INTO `tabCustom Field`", result)

    def test_filter_keeps_create_table_for_skipped_table(self):
        """filter_sql_stream keeps CREATE TABLE even for skipped tables (schema needed)."""
        from agent.backup_analyzer import filter_sql_stream

        sql = (
            "CREATE TABLE `tabActivity Log` (id int);\n"
            "INSERT INTO `tabActivity Log` VALUES (1);\n"
        )
        inp = io.StringIO(sql)
        out = io.StringIO()
        filter_sql_stream(inp, skip_tables=["tabActivity Log"], output=out)
        result = out.getvalue()

        self.assertIn("CREATE TABLE `tabActivity Log`", result)
        self.assertNotIn("INSERT INTO `tabActivity Log`", result)

    def test_filter_passes_through_unaffected_tables(self):
        """filter_sql_stream does not touch tables not in skip_tables."""
        from agent.backup_analyzer import filter_sql_stream

        sql = "INSERT INTO `tabCustom Field` VALUES (1),(2),(3);\n"
        inp = io.StringIO(sql)
        out = io.StringIO()
        filter_sql_stream(inp, skip_tables=["tabActivity Log"], output=out)

        self.assertEqual(out.getvalue(), sql)

    def test_filter_handles_multiline_insert(self):
        """filter_sql_stream skips multi-line INSERT blocks for skip_tables."""
        from agent.backup_analyzer import filter_sql_stream

        sql = (
            "INSERT INTO `tabActivity Log` VALUES\n"
            "(1,'foo'),\n"
            "(2,'bar'),\n"
            "(3,'baz');\n"
            "INSERT INTO `tabCustom Field` VALUES (10);\n"
        )
        inp = io.StringIO(sql)
        out = io.StringIO()
        filter_sql_stream(inp, skip_tables=["tabActivity Log"], output=out)
        result = out.getvalue()

        self.assertNotIn("tabActivity Log", result)
        self.assertIn("INSERT INTO `tabCustom Field`", result)

    def test_filter_skip_multiple_tables(self):
        """filter_sql_stream can skip multiple tables at once."""
        from agent.backup_analyzer import filter_sql_stream

        sql = (
            "INSERT INTO `tabActivity Log` VALUES (1);\n"
            "INSERT INTO `tabError Log` VALUES (2);\n"
            "INSERT INTO `tabCustom Field` VALUES (3);\n"
        )
        inp = io.StringIO(sql)
        out = io.StringIO()
        filter_sql_stream(
            inp,
            skip_tables=["tabActivity Log", "tabError Log"],
            output=out,
        )
        result = out.getvalue()

        self.assertNotIn("tabActivity Log", result)
        self.assertNotIn("tabError Log", result)
        self.assertIn("tabCustom Field", result)


if __name__ == "__main__":
    unittest.main()
