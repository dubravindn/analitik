from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import install_once as app


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


TARGETS = [
    app.Target(
        "worker_ai_worker",
        Path("/bundle/w.py"),
        Path("/var/lib/hermes-ai/app/hermes/ai_worker.py"),
        "old1",
        "new1",
    ),
    app.Target(
        "worker_ai_analyst",
        Path("/bundle/a.py"),
        Path("/var/lib/hermes-ai/app/hermes/ai_analyst.py"),
        "old2",
        "new2",
    ),
    app.Target(
        "renderer_ai_analyst",
        Path("/bundle/r.py"),
        Path("/opt/hermes/app/hermes/ai_analyst.py"),
        "old3",
        "new3",
    ),
]


class Fake:
    """In-memory service adapter. It never invokes systemctl or a subprocess."""

    def __init__(self, *, timer_active: bool = True):
        self.on = {
            app.BOT: True,
            app.WORKER: True,
            app.AI_TIMER: timer_active,
            app.AI_ONESHOT: False,
            app.DAILY_ONESHOT: False,
        }
        self.calls: list[tuple[str, str]] = []
        self.stable_failure: dict[str, Exception] = {}
        self.before_stable_failure = None

    def status(self, unit):
        self.calls.append(("status", unit))
        active = self.on.get(unit, False)
        return {
            "LoadState": "loaded",
            "ActiveState": "active" if active else "inactive",
            "SubState": "running" if active else "dead",
            "MainPID": "123" if active else "0",
            "NRestarts": "0",
        }

    def active(self, unit):
        self.calls.append(("active", unit))
        return self.on.get(unit, False)

    def healthy(self, unit):
        self.calls.append(("healthy", unit))
        return self.on.get(unit, False)

    def stop(self, unit):
        self.calls.append(("stop", unit))
        self.on[unit] = False

    def start(self, unit):
        self.calls.append(("start", unit))
        self.on[unit] = True

    def stable(self, unit):
        self.calls.append(("stable", unit))
        error = self.stable_failure.pop(unit, None)
        if error is not None:
            if self.before_stable_failure is not None:
                self.before_stable_failure(unit)
            raise error
        if not self.on.get(unit, False):
            raise app.Stop("fake service is not stable")

    def compile_source(self, path, interpreter):
        self.calls.append(("compile", str(path)))


class RunHarness:
    def __init__(self):
        self.current = {target.ident: target.old for target in TARGETS}
        self.rows = [{"id": target.ident} for target in TARGETS]
        self.rollback_calls = 0
        self.replace_calls: list[str] = []
        self.replace_failure_at: int | None = None
        self.queue = lambda: True
        self.backup_failure: Exception | None = None

    def digest(self, path):
        for target in TARGETS:
            if path == target.target:
                return self.current[target.ident]
        raise AssertionError(f"unexpected digest path: {path}")

    def replace(self, target, row):
        index = len(self.replace_calls)
        self.replace_calls.append(target.ident)
        if self.replace_failure_at == index:
            raise OSError("simulated replace failure")
        self.current[target.ident] = target.new

    def rollback(self, targets, rows):
        self.rollback_calls += 1
        self.assert_exact_targets(targets)
        for target in targets:
            self.current[target.ident] = target.old

    @staticmethod
    def assert_exact_targets(targets):
        if list(targets) != TARGETS:
            raise AssertionError("rollback did not receive the exact three targets")

    def backup(self, targets):
        if self.backup_failure is not None:
            raise self.backup_failure
        self.assert_exact_targets(targets)
        return self.rows

    @contextmanager
    def patches(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(app.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.object(app, "ensure_window"))
            stack.enter_context(mock.patch.object(app, "load_targets", return_value=TARGETS))
            stack.enter_context(mock.patch.object(app, "preflight_hashes"))
            stack.enter_context(mock.patch.object(app, "queue_empty", side_effect=lambda: self.queue()))
            stack.enter_context(mock.patch.object(app, "backup_dir"))
            stack.enter_context(mock.patch.object(app, "backup", side_effect=self.backup))
            stack.enter_context(mock.patch.object(app, "validate_backup"))
            stack.enter_context(mock.patch.object(app, "check_targets"))
            stack.enter_context(mock.patch.object(app, "write_state"))
            stack.enter_context(mock.patch.object(app, "replace", side_effect=self.replace))
            stack.enter_context(mock.patch.object(app, "rollback", side_effect=self.rollback))
            stack.enter_context(mock.patch.object(app, "digest", side_effect=self.digest))
            yield


class InstallerFlowTests(unittest.TestCase):
    def test_default_refusal_does_not_instantiate_real(self):
        with mock.patch.object(app, "Real") as real, mock.patch("builtins.print"):
            self.assertEqual(app.main([]), 2)
        real.assert_not_called()

    def test_success_restores_timer_only_when_initially_active(self):
        for initially_active in (True, False):
            with self.subTest(initially_active=initially_active):
                adapter = Fake(timer_active=initially_active)
                harness = RunHarness()
                with harness.patches():
                    result = app.run(adapter)
                self.assertTrue(result.startswith("PASS:"))
                self.assertEqual(harness.current, {target.ident: target.new for target in TARGETS})
                timer_stops = adapter.calls.count(("stop", app.AI_TIMER))
                timer_starts = adapter.calls.count(("start", app.AI_TIMER))
                self.assertEqual(timer_stops, int(initially_active))
                self.assertEqual(timer_starts, int(initially_active))

    def test_hash_mismatch_stops_before_any_service_stop(self):
        adapter = Fake()
        harness = RunHarness()
        with harness.patches(), mock.patch.object(
            app, "preflight_hashes", side_effect=app.Stop("hash mismatch")
        ), self.assertRaisesRegex(app.Stop, "hash mismatch"):
            app.run(adapter)
        self.assertFalse(any(action in {"start", "stop"} for action, _ in adapter.calls))

    def test_nonempty_queue_stops_before_backup_or_service_stop(self):
        adapter = Fake()
        harness = RunHarness()
        harness.queue = lambda: False
        with harness.patches(), mock.patch.object(app, "backup_dir") as backup_dir:
            with self.assertRaisesRegex(app.Stop, "queue not empty"):
                app.run(adapter)
        backup_dir.assert_not_called()
        self.assertFalse(any(action in {"start", "stop"} for action, _ in adapter.calls))

    def test_oserror_during_backup_leaves_services_untouched(self):
        adapter = Fake()
        harness = RunHarness()
        harness.backup_failure = OSError("disk full")
        with harness.patches(), self.assertRaisesRegex(OSError, "disk full"):
            app.run(adapter)
        self.assertFalse(any(action in {"start", "stop"} for action, _ in adapter.calls))
        self.assertTrue(adapter.on[app.BOT])
        self.assertTrue(adapter.on[app.WORKER])
        self.assertTrue(adapter.on[app.AI_TIMER])

    def test_oserror_during_replace_runs_actual_three_file_rollback_and_restores_timer(self):
        adapter = Fake()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            backup_dir = root / "backup"
            backup_dir.mkdir(mode=0o700)
            target_dir = root / "targets"
            source_dir = root / "sources"
            target_dir.mkdir()
            source_dir.mkdir()
            targets = []
            attrs = {}
            old_contents = {}
            for index, ident in enumerate(app.EXPECTED, start=1):
                old = f"old-live-{index}".encode()
                new = f"new-stage-{index}".encode()
                target_path = target_dir / f"{ident}.py"
                source_path = source_dir / f"{ident}.py"
                target_path.write_bytes(old)
                target_path.chmod(0o644)
                source_path.write_bytes(new)
                source_path.chmod(0o600)
                targets.append(app.Target(ident, source_path, target_path, sha(old), sha(new)))
                attrs[ident] = (os.getuid(), os.getgid(), 0o644)
                old_contents[target_path] = old

            original_safe_regular = app.safe_regular
            original_replace = app.replace
            replace_calls = []

            def local_safe_regular(path):
                result = original_safe_regular(path)
                if path.parent == backup_dir and path.name.endswith(".py"):
                    return SimpleNamespace(st_mode=result.st_mode, st_uid=0)
                return result

            def fail_second_replace(target, row):
                replace_calls.append(target.ident)
                if len(replace_calls) == 2:
                    raise OSError("simulated replace failure")
                return original_replace(target, row)

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(app.os, "geteuid", return_value=0))
                stack.enter_context(mock.patch.object(app, "ensure_window"))
                stack.enter_context(mock.patch.object(app, "load_targets", return_value=targets))
                stack.enter_context(mock.patch.object(app, "preflight_hashes"))
                stack.enter_context(mock.patch.object(app, "queue_empty", return_value=True))
                stack.enter_context(mock.patch.object(app, "backup_dir"))
                stack.enter_context(mock.patch.object(app, "BACKUP", backup_dir))
                stack.enter_context(mock.patch.object(app, "ATTRS", attrs))
                stack.enter_context(
                    mock.patch.object(app, "safe_regular", side_effect=local_safe_regular)
                )
                stack.enter_context(mock.patch.object(app, "replace", side_effect=fail_second_replace))
                with self.assertRaisesRegex(app.Stop, "installation aborted") as caught:
                    app.run(adapter)

            self.assertEqual(
                replace_calls,
                [targets[0].ident, targets[1].ident],
                repr(caught.exception.__cause__),
            )
            self.assertEqual({path: path.read_bytes() for path in old_contents}, old_contents)
            state = json.loads((backup_dir / "state.json").read_text())
            self.assertEqual(state, {"phase": "rolled-back", "replaced": []})
            self.assertTrue(adapter.on[app.BOT])
            self.assertTrue(adapter.on[app.WORKER])
            self.assertTrue(adapter.on[app.AI_TIMER])
            self.assertEqual(adapter.calls.count(("start", app.AI_TIMER)), 1)

    def test_startup_failure_rolls_back_and_restores_timer(self):
        adapter = Fake()
        adapter.stable_failure[app.BOT] = app.Stop("bot startup failed")
        harness = RunHarness()
        with harness.patches(), self.assertRaisesRegex(app.Stop, "installation aborted"):
            app.run(adapter)
        self.assertEqual(harness.rollback_calls, 1)
        self.assertEqual(harness.current, {target.ident: target.old for target in TARGETS})
        self.assertTrue(adapter.on[app.BOT])
        self.assertTrue(adapter.on[app.WORKER])
        self.assertTrue(adapter.on[app.AI_TIMER])
        self.assertEqual(adapter.calls.count(("start", app.AI_TIMER)), 1)

    def test_pending_job_on_startup_failure_does_not_stop_worker_or_rollback(self):
        adapter = Fake()
        adapter.stable_failure[app.BOT] = app.Stop("bot startup failed")
        harness = RunHarness()
        pending = {"value": False}
        harness.queue = lambda: not pending["value"]
        adapter.before_stable_failure = lambda unit: pending.update(value=True)
        with harness.patches(), self.assertRaisesRegex(app.Unsafe, "manual review required"):
            app.run(adapter)
        self.assertEqual(harness.rollback_calls, 0)
        self.assertEqual(adapter.calls.count(("stop", app.WORKER)), 1)
        self.assertTrue(adapter.on[app.WORKER])
        self.assertNotIn(("start", app.AI_TIMER), adapter.calls)


class LocalFilesystemTests(unittest.TestCase):
    def test_queue_empty_fails_closed_for_missing_and_nonempty_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            missing = root / "missing"
            with mock.patch.object(app, "SPOOL", (missing,)):
                self.assertFalse(app.queue_empty())

            queue_dirs = tuple(root / name for name in ("jobs", "processing", "results"))
            for directory in queue_dirs:
                directory.mkdir()
                directory.chmod(0o2770)
            (queue_dirs[1] / "pending.json").write_text("{}")
            original_lstat = Path.lstat

            def approved_queue_lstat(path):
                actual = original_lstat(path)
                if path in queue_dirs:
                    return SimpleNamespace(st_mode=actual.st_mode, st_uid=997, st_gid=987)
                return actual

            with mock.patch.object(app, "SPOOL", queue_dirs), mock.patch.object(
                Path, "lstat", autospec=True, side_effect=approved_queue_lstat
            ):
                self.assertFalse(app.queue_empty())

    def test_corrupted_third_backup_is_rejected_before_first_rollback_replace(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            backup_dir = root / "backup"
            target_dir = root / "targets"
            backup_dir.mkdir(mode=0o700)
            target_dir.mkdir()
            targets = []
            rows = []
            attrs = {}
            original_contents = {}
            for index, ident in enumerate(app.EXPECTED, start=1):
                old = f"old-{index}".encode()
                new = f"new-{index}".encode()
                target_path = target_dir / f"{ident}.py"
                target_path.write_bytes(new)
                target_path.chmod(0o644)
                backup_path = backup_dir / f"{ident}.py"
                backup_path.write_bytes(old if index < 3 else b"corrupted-third-backup")
                backup_path.chmod(0o600)
                target = app.Target(ident, root / f"source-{index}.py", target_path, sha(old), sha(new))
                targets.append(target)
                attrs[ident] = (os.getuid(), os.getgid(), 0o644)
                rows.append(
                    {
                        "id": ident,
                        "sha256": sha(old),
                        "uid": os.getuid(),
                        "gid": os.getgid(),
                        "mode": 0o644,
                        "target": str(target_path),
                    }
                )
                original_contents[target_path] = new
            (backup_dir / "manifest.json").write_text(json.dumps(rows, sort_keys=True))
            (backup_dir / "manifest.json").chmod(0o600)
            original_safe_regular = app.safe_regular

            def local_safe_regular(path):
                result = original_safe_regular(path)
                if path.parent == backup_dir and path.name.endswith(".py"):
                    return SimpleNamespace(st_mode=result.st_mode, st_uid=0)
                return result

            with mock.patch.object(app, "BACKUP", backup_dir), mock.patch.object(
                app, "ATTRS", attrs
            ), mock.patch.object(app, "queue_empty", return_value=True), mock.patch.object(
                app, "safe_regular", side_effect=local_safe_regular
            ), mock.patch.object(app.os, "replace", wraps=os.replace) as replace_call:
                with self.assertRaisesRegex(app.Unsafe, "backup contents invalid"):
                    app.rollback(targets, rows)
            replace_call.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in original_contents}, original_contents)

    def test_tampered_manifest_source_hash_is_rejected_even_with_expected_target_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest_path = root / "manifest.json"
            manifest_targets = []
            for index, (ident, expected) in enumerate(app.EXPECTED.items()):
                rel, target, old, new = expected
                source = root / rel
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(f"source-{index}".encode())
                manifest_targets.append(
                    {
                        "id": ident,
                        "package_path": rel,
                        "target_path": target,
                        "expected_previous_sha256": old,
                        "source_sha256": ("0" * 64 if index == 0 else new),
                        "target_sha256": new,
                    }
                )
            manifest_path.write_text(
                json.dumps({"format": "hermes-night-bundle-v1", "targets": manifest_targets})
            )
            with mock.patch.object(app, "ROOT", root), mock.patch.object(
                app, "MANIFEST", manifest_path
            ), self.assertRaisesRegex(app.Stop, "bundle hash mismatch"):
                app.load_targets()

    def test_wrong_night_date_is_rejected(self):
        class WrongDateTime:
            @classmethod
            def now(cls, tz):
                return RealDateTime(2026, 9, 13, 1, 0, tzinfo=tz)

        with mock.patch.object(app, "datetime", WrongDateTime), self.assertRaisesRegex(
            app.Stop, "safe start window closed"
        ):
            app.ensure_window()


class RealAdapterLogicTests(unittest.TestCase):
    @staticmethod
    def service_status(*, pid="123", restarts="0"):
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": pid,
            "NRestarts": restarts,
        }

    def test_stable_detects_main_pid_change_from_fake_status(self):
        adapter = app.Real()
        initial = self.service_status()
        changed = self.service_status(pid="456")
        with mock.patch.object(
            adapter, "status", side_effect=[initial, initial, changed, changed]
        ), mock.patch.object(app.time, "sleep"), self.assertRaisesRegex(
            app.Stop, "restart or instability"
        ):
            adapter.stable(app.BOT)

    def test_stable_detects_restart_counter_change_from_fake_status(self):
        adapter = app.Real()
        initial = self.service_status()
        changed = self.service_status(restarts="1")
        with mock.patch.object(
            adapter, "status", side_effect=[initial, initial, changed, changed]
        ), mock.patch.object(app.time, "sleep"), self.assertRaisesRegex(
            app.Stop, "restart or instability"
        ):
            adapter.stable(app.WORKER)


if __name__ == "__main__":
    unittest.main()
