#!/usr/bin/env python3
"""Offline tests for backup_restore_check; no PostgreSQL client is invoked."""

from __future__ import annotations

import hashlib
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import backup_restore_check as check


METADATA = {
    "table_count": 1,
    "table_names": ["public.t"],
    "columns": [],
    "index_count": 1,
    "indexes": [],
    "views": [],
    "named_views": [
        {"name": "public.nal_price_asof", "fingerprint": "a"},
        {"name": "public.purchase_price_asof", "fingerprint": "b"},
    ],
    "holiday": {"count": 1, "rowset_md5": "one"},
}
SETTINGS = {"encoding": "UTF8", "collate": "C", "ctype": "C", "locale_provider": "c", "icu_locale": ""}


class RecordingRunner:
    def __init__(self, exists="false", marker=check.MARKER):
        self.calls = []
        self.exists = exists
        self.marker = marker

    def __call__(self, args, timeout, input_text=None):
        self.calls.append((list(args), input_text))
        command = " ".join(args)
        if "SELECT EXISTS" in command:
            return check.Result(0, self.exists + "\n")
        if "shobj_description" in command:
            return check.Result(0, self.marker + "\n")
        if "pg_stat_activity" in command:
            return check.Result(0, "0\n")
        if "sha256sum" in command:
            return check.Result(0, "a" * 64 + "  hermes.custom\n")
        if "stat --format=%U:%a" in command:
            return check.Result(0, "postgres:600\n")
        return check.Result(0, "")


class BackupRestoreCheckTests(unittest.TestCase):
    def execute_mocks(self, metadata=None):
        events = []
        stack = ExitStack()
        stack.enter_context(mock.patch.object(check, "_assert_real_constants", side_effect=lambda: events.append("constants")))
        stack.enter_context(mock.patch.object(check, "_preflight", side_effect=lambda runner: events.append("preflight")))
        stack.enter_context(mock.patch.object(check, "_database_settings", side_effect=[SETTINGS, SETTINGS]))
        stack.enter_context(mock.patch.object(check, "collect_metadata", side_effect=metadata or [METADATA, METADATA, METADATA]))
        create = stack.enter_context(mock.patch.object(check, "_create_backup_dir", side_effect=lambda: events.append("mkdir")))
        dump = stack.enter_context(mock.patch.object(check, "_make_dump", return_value=Path("/private/tmp/fake.custom")))
        stack.enter_context(mock.patch.object(check, "_write_archive_hash", side_effect=lambda runner, archive: events.append("hash")))
        stack.enter_context(mock.patch.object(check, "_list_archive", side_effect=lambda runner, archive: events.append("list")))
        stack.enter_context(mock.patch.object(check, "_create_restore_database", side_effect=lambda runner, settings: events.append("create_db")))
        stack.enter_context(mock.patch.object(check, "_restore_archive", side_effect=lambda runner, archive: events.append("restore")))
        rehearse = stack.enter_context(mock.patch.object(check, "_rehearse_schema", return_value=[]))
        drop = stack.enter_context(mock.patch.object(check, "_drop_restore_database_if_marked", side_effect=lambda runner: events.append("drop")))
        read_schema = stack.enter_context(mock.patch.object(check, "_read_verified_schema", return_value="SELECT 1;\n"))
        return stack, events, create, dump, rehearse, drop, read_schema

    def test_default_path_refuses_without_subprocess(self):
        with mock.patch.object(check, "subprocess_runner") as runner:
            self.assertEqual(check.main([]), 2)
            runner.assert_not_called()

    def test_exists_accepts_postgres_boolean_spellings(self):
        for value, expected in (("true", True), ("t", True), ("false", False), ("f", False)):
            self.assertEqual(check._database_exists(RecordingRunner(value), check.RESTORE_DB), expected)

    def test_new_backup_directory_is_exclusive_and_owned_by_postgres(self):
        created = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=997, st_gid=987)
        with mock.patch.object(check.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=997, pw_gid=987)), \
             mock.patch.object(check.os, "mkdir") as mkdir, \
             mock.patch.object(check.os, "chown") as chown, \
             mock.patch.object(check.os, "chmod") as chmod, \
             mock.patch.object(check.os, "lstat", return_value=created):
            check._create_backup_dir()
        mkdir.assert_called_once_with(check.BACKUP_DIR, 0o700)
        chown.assert_called_once_with(check.BACKUP_DIR, 997, 987)
        chmod.assert_called_once_with(check.BACKUP_DIR, 0o700)

    def test_existing_backup_path_refuses_before_any_write(self):
        parent = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        with mock.patch.object(check.os, "geteuid", return_value=0), \
             mock.patch.object(check.os, "lstat", return_value=parent), \
             mock.patch.object(check.os.path, "lexists", return_value=True), \
             mock.patch.object(check.os, "mkdir") as mkdir:
            with self.assertRaises(check.CheckError):
                check._assert_real_constants()
        mkdir.assert_not_called()

    def test_restore_is_fixed_to_temporary_database(self):
        runner = RecordingRunner()
        check._restore_archive(runner, Path("/private/tmp/fake.custom"))
        args = runner.calls[0][0]
        self.assertIn(f"--dbname={check.RESTORE_DB}", args)
        self.assertNotIn(f"--dbname={check.SOURCE_DB}", args)
        for flag in ("--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"):
            self.assertIn(flag, args)

    def test_rehearsal_uses_verified_content_over_stdin(self):
        runner = RecordingRunner()
        with mock.patch.object(check, "collect_metadata", return_value=METADATA):
            self.assertEqual(check._rehearse_schema(runner, "SELECT 1;\n", METADATA), [])
        args, input_text = runner.calls[0]
        self.assertIn("--file=-", args)
        self.assertNotIn("schema.sql", args)
        self.assertIn("SET LOCAL lock_timeout", input_text)
        self.assertIn("SET LOCAL statement_timeout", input_text)

    def test_schema_refuses_meta_command_even_at_expected_hash(self):
        with tempfile.TemporaryDirectory(prefix="night-backup-test-", dir=check.SAFE_TEMP_PARENT) as directory:
            schema = Path(directory) / "schema.sql"
            schema.write_text("  \\connect hermes\n", encoding="utf-8")
            original = check.EXPECTED_SCHEMA_SHA256
            check.EXPECTED_SCHEMA_SHA256 = hashlib.sha256(schema.read_bytes()).hexdigest()
            try:
                with self.assertRaises(check.CheckError):
                    check._read_verified_schema(schema)
            finally:
                check.EXPECTED_SCHEMA_SHA256 = original

    def test_execute_success_runs_cleanup(self):
        stack, events, _create, _dump, _rehearse, drop, _read = self.execute_mocks()
        with stack:
            result = check.execute(RecordingRunner(), None)
        self.assertTrue(result.startswith("PASS:"))
        drop.assert_called_once()
        self.assertEqual(events, ["constants", "preflight", "mkdir", "hash", "list", "create_db", "restore", "drop"])

    def test_database_already_exists_refuses_before_backup_write(self):
        stack, _events, create, _dump, _rehearse, drop, _read = self.execute_mocks()
        with stack, mock.patch.object(check, "_preflight", side_effect=check.CheckError("temporary exists")):
            with self.assertRaises(check.CheckError):
                check.execute(RecordingRunner(), None)
        create.assert_not_called()
        drop.assert_not_called()

    def test_pg_dump_failure_retains_no_cleanup(self):
        stack, _events, _create, dump, _rehearse, drop, _read = self.execute_mocks()
        dump.side_effect = check.CheckError("pg_dump failed")
        with stack:
            with self.assertRaises(check.CheckError):
                check.execute(RecordingRunner(), None)
        drop.assert_not_called()

    def test_source_metadata_drift_retains_temporary_database(self):
        changed = dict(METADATA, holiday={"count": 2, "rowset_md5": "two"})
        stack, _events, _create, _dump, _rehearse, drop, _read = self.execute_mocks([METADATA, METADATA, changed])
        with stack:
            with self.assertRaises(check.Inconclusive):
                check.execute(RecordingRunner(), None)
        drop.assert_not_called()

    def test_marker_mismatch_prevents_drop(self):
        runner = RecordingRunner(marker="wrong-marker")
        with self.assertRaises(check.Inconclusive):
            check._drop_restore_database_if_marked(runner)
        self.assertFalse(any("DROP DATABASE" in " ".join(args) for args, _ in runner.calls))

    def test_database_marker_uses_shared_catalog_description(self):
        runner = RecordingRunner()
        check._drop_restore_database_if_marked(runner)
        marker_query = " ".join(runner.calls[0][0])
        self.assertIn("shobj_description(oid, 'pg_database')", marker_query)
        self.assertNotIn("COALESCE(obj_description(", marker_query)
        self.assertTrue(any("DROP DATABASE" in " ".join(args) for args, _ in runner.calls))

    def test_rehearsal_change_retains_temporary_database(self):
        stack, _events, _create, _dump, rehearse, drop, read_schema = self.execute_mocks()
        rehearse.return_value = ["views"]
        with stack:
            with self.assertRaises(check.Inconclusive):
                check.execute(RecordingRunner(), Path("/irrelevant/schema.sql"))
        read_schema.assert_called_once()
        drop.assert_not_called()

    def test_metadata_diff_reports_category_not_values(self):
        before = {"holiday": {"count": 1, "rowset_md5": "one"}, "views": []}
        after = {"holiday": {"count": 2, "rowset_md5": "two"}, "views": []}
        self.assertEqual(check.metadata_differences(before, after), ["holiday"])


if __name__ == "__main__":
    unittest.main()
