#!/usr/bin/env python3
"""Verify an offline three-file bundle; self-test only writes a fresh temp tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.json"
SAFE_TEMP_PARENT = Path("/private/tmp").resolve()
REQUIRED_IDS = (
    "worker_ai_worker",
    "worker_ai_analyst",
    "renderer_ai_analyst",
)
EXPECTED_ROUTES = {
    "worker_ai_worker": (
        "08_НАПРАВЛЕНИЯ/B04/local_compatibility_check/patched_full/hermes/ai_worker.py",
        "files/worker/hermes/ai_worker.py",
        "/var/lib/hermes-ai/app/hermes/ai_worker.py",
        "var/lib/hermes-ai/app/hermes/ai_worker.py",
    ),
    "worker_ai_analyst": (
        "08_НАПРАВЛЕНИЯ/B04/local_compatibility_check/patched_full/hermes/ai_analyst.py",
        "files/worker/hermes/ai_analyst.py",
        "/var/lib/hermes-ai/app/hermes/ai_analyst.py",
        "var/lib/hermes-ai/app/hermes/ai_analyst.py",
    ),
    "renderer_ai_analyst": (
        "07_ЧАТЫ/renderer_compatibility_check/patched/hermes/ai_analyst.py",
        "files/renderer/hermes/ai_analyst.py",
        "/opt/hermes/app/hermes/ai_analyst.py",
        "opt/hermes/app/hermes/ai_analyst.py",
    ),
}


class BundleError(RuntimeError):
    """The package or synthetic operation does not meet an invariant."""


class SourceMismatch(BundleError):
    """A synthetic target does not have the expected pre-replacement bytes."""


class BackupIntegrityError(BundleError):
    """A synthetic backup no longer matches the recorded bytes."""


@dataclass(frozen=True)
class BackupRecord:
    target: Path
    backup: Path
    sha256: str
    mode: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("format") != "hermes-night-bundle-v1":
        raise BundleError("unexpected manifest format")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or len(targets) != 3:
        raise BundleError("manifest must describe exactly three targets")
    if tuple(item.get("id") for item in targets) != REQUIRED_IDS:
        raise BundleError("manifest target IDs or order do not match the approved bundle")
    return manifest


def validate_manifest_and_package(manifest: dict[str, Any]) -> None:
    targets = manifest["targets"]
    required_fields = {
        "id",
        "source_path",
        "package_path",
        "target_path",
        "synthetic_relative_path",
        "expected_previous_sha256",
        "source_sha256",
        "target_sha256",
    }
    target_paths = set()
    package_paths = set()
    analyst_hashes: dict[str, str] = {}

    for item in targets:
        missing = required_fields.difference(item)
        if missing:
            raise BundleError(f"{item.get('id', '<unknown>')}: missing {sorted(missing)}")
        expected_route = EXPECTED_ROUTES[item["id"]]
        actual_route = (
            item["source_path"],
            item["package_path"],
            item["target_path"],
            item["synthetic_relative_path"],
        )
        if actual_route != expected_route:
            raise BundleError(f"{item['id']}: source/package/target route was altered")
        package_rel = Path(item["package_path"])
        if package_rel.is_absolute() or ".." in package_rel.parts:
            raise BundleError(f"{item['id']}: unsafe package path")
        package_file = PACKAGE_ROOT / package_rel
        if not package_file.is_file():
            raise BundleError(f"{item['id']}: package file is absent")
        if package_file.suffix != ".py":
            raise BundleError(f"{item['id']}: package target is not Python")
        if sha256_file(package_file) != item["source_sha256"]:
            raise BundleError(f"{item['id']}: source/package SHA-256 mismatch")
        if item["source_sha256"] != item["target_sha256"]:
            raise BundleError(f"{item['id']}: package and target SHA-256 differ")
        try:
            compile(package_file.read_text(encoding="utf-8"), str(package_file), "exec")
        except SyntaxError as error:
            raise BundleError(f"{item['id']}: syntax error: {error}") from error
        target_paths.add(item["target_path"])
        package_paths.add(item["package_path"])
        if item["id"].endswith("ai_analyst"):
            analyst_hashes[item["id"]] = item["target_sha256"]

    if len(target_paths) != 3 or len(package_paths) != 3:
        raise BundleError("targets and package paths must each be unique")
    if analyst_hashes["worker_ai_analyst"] == analyst_hashes["renderer_ai_analyst"]:
        raise BundleError("worker and renderer ai_analyst artifacts were confused")


def _synthetic_target(root: Path, item: dict[str, Any]) -> Path:
    """Map manifest data only inside the fresh test root; never use target_path."""
    if (
        not root.name.startswith("night-bundle-test-")
        or root.resolve().parent != SAFE_TEMP_PARENT
    ):
        raise BundleError("synthetic write guard rejected a non-approved temporary root")
    relative = Path(item["synthetic_relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise BundleError("unsafe synthetic path")
    target = root / relative
    if root not in target.parents:
        raise BundleError("synthetic target escaped its root")
    return target


def _apply_in_synthetic_tree(
    root: Path, manifest: dict[str, Any]
) -> list[BackupRecord]:
    """Exercise replacement locally. This internal helper has no production path input."""
    prepared: list[tuple[dict[str, Any], Path, bytes, int]] = []
    for item in manifest["targets"]:
        target = _synthetic_target(root, item)
        if not target.is_file():
            raise SourceMismatch(f"{item['id']}: synthetic target is absent")
        current = target.read_bytes()
        if sha256_bytes(current) != item["expected_previous_sha256"]:
            raise SourceMismatch(f"{item['id']}: expected old SHA-256 does not match")
        prepared.append((item, target, current, stat.S_IMODE(target.stat().st_mode)))

    backup_dir = root / "synthetic-backup"
    backup_dir.mkdir(mode=0o700)
    records: list[BackupRecord] = []
    for item, target, previous, mode in prepared:
        backup = backup_dir / f"{item['id']}.bak"
        backup.write_bytes(previous)
        os.chmod(backup, mode)
        records.append(BackupRecord(target, backup, sha256_bytes(previous), mode))

    for item, target, _previous, mode in prepared:
        source = PACKAGE_ROOT / item["package_path"]
        replacement = source.read_bytes()
        temporary = target.with_name(f".{target.name}.{item['id']}.tmp")
        temporary.write_bytes(replacement)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        if sha256_file(target) != item["target_sha256"]:
            raise BundleError(f"{item['id']}: synthetic replacement hash failed")
    return records


def _restore_in_synthetic_tree(records: list[BackupRecord]) -> None:
    """Refuse a corrupt backup before replacing any synthetic target."""
    for record in records:
        if not record.backup.is_file() or sha256_file(record.backup) != record.sha256:
            raise BackupIntegrityError(f"corrupt synthetic backup: {record.backup.name}")
    for record in records:
        temporary = record.target.with_name(f".{record.target.name}.restore.tmp")
        temporary.write_bytes(record.backup.read_bytes())
        os.chmod(temporary, record.mode)
        os.replace(temporary, record.target)


def _synthetic_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Use unique synthetic old bytes while retaining real packaged replacements."""
    fixture = json.loads(json.dumps(manifest))
    old_bytes: dict[str, bytes] = {}
    for item in fixture["targets"]:
        prior = f"synthetic previous bytes for {item['id']}\n".encode("ascii")
        old_bytes[item["id"]] = prior
        item["expected_previous_sha256"] = sha256_bytes(prior)
    return fixture, old_bytes


def _make_synthetic_tree(root: Path, manifest: dict[str, Any], old: dict[str, bytes]) -> dict[Path, tuple[bytes, int]]:
    snapshot: dict[Path, tuple[bytes, int]] = {}
    for index, item in enumerate(manifest["targets"]):
        target = _synthetic_target(root, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old[item["id"]])
        mode = (0o640, 0o600, 0o644)[index]
        os.chmod(target, mode)
        snapshot[target] = (old[item["id"]], mode)
    sentinel = root / "unrelated.txt"
    sentinel.write_bytes(b"must remain untouched\n")
    os.chmod(sentinel, 0o640)
    snapshot[sentinel] = (sentinel.read_bytes(), stat.S_IMODE(sentinel.stat().st_mode))
    return snapshot


def run_self_test(manifest: dict[str, Any]) -> None:
    fixture, old = _synthetic_manifest(manifest)
    with tempfile.TemporaryDirectory(
        prefix="night-bundle-test-", dir=SAFE_TEMP_PARENT
    ) as directory:
        root = Path(directory)
        before = _make_synthetic_tree(root, fixture, old)
        records = _apply_in_synthetic_tree(root, fixture)
        changed = {
            record.target
            for record in records
            if record.target.read_bytes() != before[record.target][0]
        }
        expected = {_synthetic_target(root, item) for item in fixture["targets"]}
        if changed != expected or len(records) != 3:
            raise BundleError("synthetic replacement did not change exactly three targets")
        sentinel = root / "unrelated.txt"
        if (sentinel.read_bytes(), stat.S_IMODE(sentinel.stat().st_mode)) != before[sentinel]:
            raise BundleError("synthetic replacement touched the sentinel")
        _restore_in_synthetic_tree(records)
        for target, expected_state in before.items():
            actual = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
            if actual != expected_state:
                raise BundleError(f"synthetic restore did not recover {target.name}")

    with tempfile.TemporaryDirectory(
        prefix="night-bundle-test-", dir=SAFE_TEMP_PARENT
    ) as directory:
        root = Path(directory)
        before = _make_synthetic_tree(root, fixture, old)
        bad_target = _synthetic_target(root, fixture["targets"][1])
        bad_target.write_bytes(b"unexpected current bytes\n")
        try:
            _apply_in_synthetic_tree(root, fixture)
        except SourceMismatch:
            pass
        else:
            raise BundleError("mismatched synthetic source was accepted")
        for target, expected_state in before.items():
            if target == bad_target:
                continue
            actual = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
            if actual != expected_state:
                raise BundleError("mismatch changed a target before refusing")

    with tempfile.TemporaryDirectory(
        prefix="night-bundle-test-", dir=SAFE_TEMP_PARENT
    ) as directory:
        root = Path(directory)
        _make_synthetic_tree(root, fixture, old)
        records = _apply_in_synthetic_tree(root, fixture)
        records[0].backup.write_bytes(b"corrupted synthetic backup\n")
        first_target_after_apply = records[0].target.read_bytes()
        try:
            _restore_in_synthetic_tree(records)
        except BackupIntegrityError:
            pass
        else:
            raise BundleError("corrupted synthetic backup was accepted")
        if records[0].target.read_bytes() != first_target_after_apply:
            raise BundleError("corrupt backup changed a target before refusing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "self-test"))
    args = parser.parse_args()
    manifest = load_manifest()
    validate_manifest_and_package(manifest)
    if args.command == "self-test":
        run_self_test(manifest)
        print("PASS: package verified; synthetic 3-file replace/restore/mismatch/corrupt-backup tests")
    else:
        print("PASS: manifest, three package hashes, distinct ai_analyst targets, syntax-only compile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
