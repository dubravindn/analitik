#!/usr/bin/env python3
"""Future-only PostgreSQL backup/restore check for the Hermes maintenance plan.

With no flags this script only prints its refusal and never starts a subprocess.
It deliberately has no host, port, password, database, backup-directory, or
target-name options.  The only execution path is the exact, separately gated
local PostgreSQL procedure described in README_DB.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


SOURCE_DB = "hermes"
RESTORE_DB = "hermes_restorecheck_20260912_4cb3"
BACKUP_DIR = Path("/var/backups/hermes-night-20260912-4cb3")
ARCHIVE_NAME = "hermes.custom"
ARCHIVE_SHA_NAME = "hermes.custom.sha256"
TOC_NAME = "hermes.custom.toc"
MARKER = "hermes-night-restorecheck:20260912-4cb3"
EXPECTED_SCHEMA_SHA256 = "a27afe9dae79a188de93f0bbd9a970d911e175a86a149ee29bd43fd48690e7ad"
LOCAL_SOCKET_DIR = "/var/run/postgresql"
SAFE_TEMP_PARENT = Path("/private/tmp").resolve()
COMMAND_TIMEOUT_SECONDS = 600
LOCK_TIMEOUT = "10s"


class CheckError(RuntimeError):
    """A guarded check could not safely continue."""


class Inconclusive(CheckError):
    """Evidence changed or is incomplete; retain created artefacts for review."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], int, Optional[str]], Result]


def _safe_env() -> dict[str, str]:
    """Do not inherit PG* variables, service files, passwords, or remote hosts."""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}


def subprocess_runner(args: Sequence[str], timeout: int, input_text: str | None = None) -> Result:
    completed = subprocess.run(
        list(args),
        check=False,
        shell=False,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=_safe_env(),
    )
    return Result(completed.returncode, completed.stdout, completed.stderr)


def _sudo_postgres(*command: str) -> list[str]:
    return ["sudo", "-n", "-u", "postgres", "--", *command]


def _run(runner: Runner, args: Sequence[str], label: str, input_text: str | None = None) -> str:
    try:
        result = runner(args, COMMAND_TIMEOUT_SECONDS, input_text)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"{label} did not complete safely") from error
    if result.returncode != 0:
        # stderr can contain object names, paths, or client diagnostics: never emit it.
        raise CheckError(f"{label} failed; no production change is attempted")
    return result.stdout.strip()


def _psql(runner: Runner, database: str, sql: str, label: str) -> str:
    return _run(
        runner,
        _sudo_postgres(
            "psql",
            "--no-psqlrc",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=on",
            f"--host={LOCAL_SOCKET_DIR}",
            "--username=postgres",
            f"--dbname={database}",
            "--command",
            sql,
        ),
        label,
    )


def _one_json(runner: Runner, database: str, sql: str, label: str) -> object:
    output = _psql(runner, database, sql, label)
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise CheckError(f"{label} did not return expected metadata") from error


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise CheckError("internal database identifier validation failed")
    return '"' + value + '"'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_real_constants() -> None:
    if BACKUP_DIR != Path("/var/backups/hermes-night-20260912-4cb3"):
        raise CheckError("backup directory was altered")
    if SOURCE_DB != "hermes" or RESTORE_DB != "hermes_restorecheck_20260912_4cb3":
        raise CheckError("database target was altered")
    if os.geteuid() != 0:
        raise CheckError("real execution requires root for exact protected backup ownership")
    try:
        parent_stat = os.lstat(BACKUP_DIR.parent)
    except OSError as error:
        raise CheckError("backup parent cannot be inspected") from error
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode) or parent_stat.st_uid != 0:
        raise CheckError("backup parent must be a real root-owned directory")
    if os.path.lexists(BACKUP_DIR):
        raise CheckError("new backup directory already exists; refusing to overwrite it")


def _metadata_sql() -> str:
    """No values are printed: JSON is compared internally and reported by category."""
    return """
WITH user_relations AS (
  SELECT c.oid, n.nspname, c.relname, c.relkind
  FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg_toast%'
), table_rows AS (
  SELECT nspname, relname FROM user_relations WHERE relkind IN ('r', 'p')
), column_rows AS (
  SELECT r.nspname, r.relname, a.attnum, a.attname,
         format_type(a.atttypid, a.atttypmod) AS type_name, a.attnotnull,
         COALESCE(pg_get_expr(ad.adbin, ad.adrelid), '') AS default_expr
  FROM user_relations AS r
  JOIN pg_attribute AS a ON a.attrelid = r.oid
  LEFT JOIN pg_attrdef AS ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
  WHERE r.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
), index_rows AS (
  SELECT tn.nspname AS table_schema, tc.relname AS table_name,
         inx.relname AS index_name, pg_get_indexdef(i.indexrelid) AS definition
  FROM pg_index AS i
  JOIN pg_class AS inx ON inx.oid = i.indexrelid
  JOIN pg_class AS tc ON tc.oid = i.indrelid
  JOIN pg_namespace AS tn ON tn.oid = tc.relnamespace
  WHERE tn.nspname NOT IN ('pg_catalog', 'information_schema')
    AND tn.nspname NOT LIKE 'pg_toast%'
), view_rows AS (
  SELECT nspname, relname, pg_get_viewdef(oid, true) AS definition
  FROM user_relations WHERE relkind = 'v'
), named_views AS (
  SELECT nspname || '.' || relname AS name, md5(definition) AS fingerprint
  FROM view_rows WHERE relname IN ('nal_price_asof', 'purchase_price_asof')
), holiday_rows AS (
  SELECT md5(to_jsonb(h)::text) AS row_hash FROM public.holiday AS h
)
SELECT jsonb_build_object(
  'table_count', (SELECT count(*) FROM table_rows),
  'table_names', COALESCE((SELECT jsonb_agg(nspname || '.' || relname ORDER BY nspname, relname) FROM table_rows), '[]'::jsonb),
  'columns', COALESCE((SELECT jsonb_agg(jsonb_build_object('table', nspname || '.' || relname, 'position', attnum, 'name', attname, 'type', type_name, 'not_null', attnotnull, 'default', default_expr) ORDER BY nspname, relname, attnum) FROM column_rows), '[]'::jsonb),
  'index_count', (SELECT count(*) FROM index_rows),
  'indexes', COALESCE((SELECT jsonb_agg(jsonb_build_object('table', table_schema || '.' || table_name, 'name', index_name, 'definition', definition) ORDER BY table_schema, table_name, index_name) FROM index_rows), '[]'::jsonb),
  'views', COALESCE((SELECT jsonb_agg(jsonb_build_object('name', nspname || '.' || relname, 'definition', definition) ORDER BY nspname, relname) FROM view_rows), '[]'::jsonb),
  'named_views', COALESCE((SELECT jsonb_agg(jsonb_build_object('name', name, 'fingerprint', fingerprint) ORDER BY name) FROM named_views), '[]'::jsonb),
  'holiday', (SELECT jsonb_build_object('count', count(*), 'rowset_md5', md5(COALESCE(string_agg(row_hash, '' ORDER BY row_hash), ''))) FROM holiday_rows)
)::text;
"""


def collect_metadata(runner: Runner, database: str, label: str) -> dict[str, object]:
    value = _one_json(runner, database, _metadata_sql(), label)
    if not isinstance(value, dict):
        raise CheckError(f"{label} metadata is not an object")
    names = value.get("named_views")
    if (
        not isinstance(names, list)
        or len(names) != 2
        or {item.get("name", "").split(".")[-1] for item in names if isinstance(item, dict)}
        != {"nal_price_asof", "purchase_price_asof"}
    ):
        raise CheckError(f"{label} does not contain exactly the two expected view fingerprints")
    return value


def metadata_differences(before: dict[str, object], after: dict[str, object]) -> list[str]:
    # Values themselves, including rows and definitions, are never reported.
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def _database_exists(runner: Runner, database: str) -> bool:
    sql = f"SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = {_sql_literal(database)})::text;"
    value = _psql(runner, "postgres", sql, "temporary database existence check").lower()
    if value not in {"t", "f", "true", "false"}:
        raise CheckError("temporary database existence check was ambiguous")
    return value in {"t", "true"}


def _database_settings(runner: Runner, database: str, label: str) -> dict[str, object]:
    sql = f"""
SELECT jsonb_build_object(
  'encoding', pg_encoding_to_char(encoding),
  'collate', datcollate,
  'ctype', datctype,
  'locale_provider', datlocprovider,
  'icu_locale', COALESCE(daticulocale, '')
)::text
FROM pg_database WHERE datname = {_sql_literal(database)};
"""
    value = _one_json(runner, "postgres", sql, label)
    if not isinstance(value, dict) or set(value) != {
        "encoding", "collate", "ctype", "locale_provider", "icu_locale"
    }:
        raise CheckError(f"{label} did not return complete database settings")
    return value


def _preflight(runner: Runner) -> int:
    user = _run(runner, _sudo_postgres("id", "-un"), "postgres user check")
    if user != "postgres":
        raise CheckError("sudo did not resolve to postgres")
    version = _psql(runner, "postgres", "SHOW server_version;", "PostgreSQL version check")
    if not version.startswith("16."):
        raise CheckError("PostgreSQL 16 is required for this checked procedure")
    size = _psql(
        runner,
        "postgres",
        f"SELECT pg_database_size({_sql_literal(SOURCE_DB)})::text;",
        "source database size check",
    )
    try:
        size_bytes = int(size)
    except ValueError as error:
        raise CheckError("source database size was not numeric") from error
    available = _run(runner, _sudo_postgres("df", "--output=avail", "-B1", "/var/backups"), "free-space check")
    lines = [line.strip() for line in available.splitlines() if line.strip()]
    try:
        free_bytes = int(lines[-1])
    except (IndexError, ValueError) as error:
        raise CheckError("free-space check was not numeric") from error
    if free_bytes < size_bytes * 3 + 1024 * 1024 * 1024:
        raise CheckError("insufficient free space for dump plus isolated restore")
    if _database_exists(runner, RESTORE_DB):
        raise CheckError("temporary database already exists; refusing to touch it")
    return size_bytes


def _create_backup_dir() -> None:
    """Exclusively create the exact new directory; never use mkdir -p/install -d."""
    try:
        postgres = pwd.getpwnam("postgres")
        os.mkdir(BACKUP_DIR, 0o700)
    except FileExistsError as error:
        raise CheckError("new backup directory appeared during creation; refusing to overwrite it") from error
    except OSError as error:
        raise CheckError("new backup directory could not be created") from error
    try:
        os.chown(BACKUP_DIR, postgres.pw_uid, postgres.pw_gid)
        os.chmod(BACKUP_DIR, 0o700)
        created = os.lstat(BACKUP_DIR)
    except OSError as error:
        raise CheckError("new backup directory ownership or mode could not be secured") from error
    if (
        not stat.S_ISDIR(created.st_mode)
        or stat.S_ISLNK(created.st_mode)
        or created.st_uid != postgres.pw_uid
        or created.st_gid != postgres.pw_gid
        or stat.S_IMODE(created.st_mode) != 0o700
    ):
        raise CheckError("new backup directory owner or mode is unsafe")


def _make_dump(runner: Runner) -> Path:
    archive = BACKUP_DIR / ARCHIVE_NAME
    _run(
        runner,
        _sudo_postgres(
            "pg_dump",
            "--format=custom",
            f"--lock-wait-timeout={LOCK_TIMEOUT}",
            f"--host={LOCAL_SOCKET_DIR}",
            "--username=postgres",
            f"--file={archive}",
            SOURCE_DB,
        ),
        "custom pg_dump",
    )
    _run(runner, _sudo_postgres("chmod", "600", str(archive)), "dump mode setting")
    info = _run(runner, _sudo_postgres("stat", "--format=%U:%a", str(archive)), "dump mode check")
    if info != "postgres:600":
        raise CheckError("dump owner or mode is unsafe")
    return archive


def _write_archive_hash(runner: Runner, archive: Path) -> None:
    # Both reading the 0600 archive and writing its manifest stay under postgres.
    # `tee` receives the non-secret checksum over stdin; there is no shell/redirection.
    digest_line = _run(runner, _sudo_postgres("sha256sum", str(archive)), "dump SHA-256 calculation")
    digest = digest_line.split(maxsplit=1)[0] if digest_line else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CheckError("dump SHA-256 output was malformed")
    manifest = BACKUP_DIR / ARCHIVE_SHA_NAME
    _run(
        runner,
        _sudo_postgres("tee", str(manifest)),
        "hash manifest creation",
        f"{digest}  {ARCHIVE_NAME}\n",
    )
    _run(runner, _sudo_postgres("chmod", "600", str(manifest)), "hash manifest mode setting")
    info = _run(runner, _sudo_postgres("stat", "--format=%U:%a", str(manifest)), "hash manifest mode check")
    if info != "postgres:600":
        raise CheckError("hash manifest owner or mode is unsafe")


def _list_archive(runner: Runner, archive: Path) -> None:
    toc = BACKUP_DIR / TOC_NAME
    _run(runner, _sudo_postgres("pg_restore", "--list", f"--file={toc}", str(archive)), "pg_restore archive list")
    _run(runner, _sudo_postgres("chmod", "600", str(toc)), "archive list mode setting")
    info = _run(runner, _sudo_postgres("stat", "--format=%U:%a", str(toc)), "archive list mode check")
    if info != "postgres:600":
        raise CheckError("archive list owner or mode is unsafe")


def _create_restore_database(runner: Runner, source_settings: dict[str, object]) -> None:
    target = _sql_identifier(RESTORE_DB)
    _psql(
        runner,
        "postgres",
        f"CREATE DATABASE {target} TEMPLATE template0 CONNECTION LIMIT 0;",
        "temporary database creation",
    )
    _psql(runner, "postgres", f"REVOKE CONNECT ON DATABASE {target} FROM PUBLIC;", "temporary database access restriction")
    _psql(runner, "postgres", f"COMMENT ON DATABASE {target} IS {_sql_literal(MARKER)};", "temporary database marker")
    restored_settings = _database_settings(runner, RESTORE_DB, "temporary database settings check")
    if restored_settings != source_settings:
        raise Inconclusive("temporary database locale or encoding differs; retaining it")


def _restore_archive(runner: Runner, archive: Path) -> None:
    _run(
        runner,
        _sudo_postgres(
            "pg_restore",
            f"--host={LOCAL_SOCKET_DIR}",
            "--username=postgres",
            f"--dbname={RESTORE_DB}",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--single-transaction",
            str(archive),
        ),
        "isolated pg_restore",
    )


def _read_verified_schema(schema_file: Path) -> str:
    try:
        raw = schema_file.read_bytes()
    except OSError as error:
        raise CheckError("schema.sql is absent or unreadable") from error
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SCHEMA_SHA256:
        raise CheckError("schema.sql is absent or its SHA-256 does not match the approved source")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CheckError("schema.sql is not UTF-8") from error
    if re.search(r"(?m)^\s*\\", source):
        raise CheckError("schema.sql contains a psql meta-command; rehearsal is refused")
    if re.search(r"(?im)^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", source):
        raise CheckError("schema.sql contains transaction control; rehearsal is refused")
    return source


def _rehearse_schema(runner: Runner, schema_source: str, before: dict[str, object]) -> list[str]:
    input_sql = "SET LOCAL lock_timeout = '15s';\nSET LOCAL statement_timeout = '60s';\n" + schema_source
    _run(
        runner,
        _sudo_postgres(
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=on",
            "--single-transaction",
            f"--host={LOCAL_SOCKET_DIR}",
            "--username=postgres",
            f"--dbname={RESTORE_DB}",
            "--file=-",
        ),
        "temporary database schema rehearsal",
        input_sql,
    )
    after = collect_metadata(runner, RESTORE_DB, "temporary metadata after schema rehearsal")
    return metadata_differences(before, after)


def _drop_restore_database_if_marked(runner: Runner) -> None:
    target = _sql_identifier(RESTORE_DB)
    marker = _psql(
        runner,
        "postgres",
        f"SELECT COALESCE(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = {_sql_literal(RESTORE_DB)};",
        "temporary database marker check",
    )
    if marker != MARKER:
        raise Inconclusive("temporary database marker mismatch; retaining it")
    connections = _psql(
        runner,
        "postgres",
        f"SELECT count(*)::text FROM pg_stat_activity WHERE datname = {_sql_literal(RESTORE_DB)} AND pid <> pg_backend_pid();",
        "temporary database connection check",
    )
    if connections != "0":
        raise Inconclusive("temporary database has connections; retaining it")
    _psql(runner, "postgres", f"DROP DATABASE {target};", "marked temporary database deletion")


def execute(runner: Runner, schema_file: Path | None) -> str:
    """Run the future action. Any error retains archive and any created test DB."""
    schema_source = _read_verified_schema(schema_file) if schema_file is not None else None
    _assert_real_constants()
    _preflight(runner)
    source_settings = _database_settings(runner, SOURCE_DB, "source database settings check")
    source_before = collect_metadata(runner, SOURCE_DB, "source metadata before dump")
    _create_backup_dir()
    old_umask = os.umask(0o077)
    try:
        archive = _make_dump(runner)
        _write_archive_hash(runner, archive)
        _list_archive(runner, archive)
    finally:
        os.umask(old_umask)
    created_restore_db = False
    _create_restore_database(runner, source_settings)
    created_restore_db = True
    _restore_archive(runner, archive)
    restored_before = collect_metadata(runner, RESTORE_DB, "restored metadata before rehearsal")
    source_after = collect_metadata(runner, SOURCE_DB, "source metadata after restore")
    drift = metadata_differences(source_before, source_after)
    if drift:
        raise Inconclusive("source metadata drifted during check; retaining dump and temporary database")
    restore_diff = metadata_differences(source_before, restored_before)
    if restore_diff:
        raise Inconclusive("restored metadata differs from source; retaining dump and temporary database")
    if schema_source is not None:
        rehearsal_diff = _rehearse_schema(runner, schema_source, restored_before)
        if rehearsal_diff:
            raise Inconclusive("schema rehearsal has semantic metadata changes; retaining dump and temporary database")
    if not created_restore_db:
        raise CheckError("internal safeguard: temporary database was not created by this attempt")
    _drop_restore_database_if_marked(runner)
    return "PASS: isolated backup/restore verification completed; dump retained, marked test database removed"


class FakeRunner:
    """No-subprocess test double. Responses are deliberately metadata-only."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: int, input_text: str | None = None) -> Result:
        self.calls.append(list(args))
        command = " ".join(args)
        if " id -un" in command:
            return Result(0, "postgres\n")
        if "SHOW server_version" in command:
            return Result(0, "16.15\n")
        if "pg_database_size" in command:
            return Result(0, "149946368\n")
        if "df --output=avail -B1" in command:
            return Result(0, "Avail\n25769803776\n")
        if "SELECT EXISTS" in command:
            return Result(0, "f\n")
        if "pg_database WHERE datname" in command and "shobj_description" in command:
            return Result(0, MARKER + "\n")
        if "pg_stat_activity" in command:
            return Result(0, "0\n")
        if "stat --format=%U:%a" in command:
            return Result(0, "postgres:600\n" if ARCHIVE_NAME in command or TOC_NAME in command or ARCHIVE_SHA_NAME in command else "postgres:700\n")
        if "_metadata_sql" in command:
            return Result(0, "{}\n")
        return Result(0, "")


def _self_test() -> str:
    """Exercise guards, command construction, metadata diff, and temp filesystem only."""
    if _safe_env().keys() != {"PATH", "LANG", "LC_ALL"}:
        raise AssertionError("unsafe subprocess environment")
    before = {"table_names": ["public.t"], "columns": [], "indexes": [], "views": [], "named_views": [], "holiday": {"count": 1, "rowset_md5": "x"}}
    if metadata_differences(before, dict(before)):
        raise AssertionError("equal metadata compared differently")
    if metadata_differences(before, {**before, "holiday": {"count": 2, "rowset_md5": "y"}}) != ["holiday"]:
        raise AssertionError("holiday drift was not categorized")
    with tempfile.TemporaryDirectory(prefix="night-backup-test-", dir=SAFE_TEMP_PARENT) as directory:
        root = Path(directory)
        schema = root / "schema.sql"
        schema.write_text("SELECT 1;\n", encoding="utf-8")
        try:
            _read_verified_schema(schema)
        except CheckError:
            pass
        else:
            raise AssertionError("unexpected schema SHA was accepted")
        bad = root / "bad.sql"
        bad.write_text("\\connect hermes\n", encoding="utf-8")
        original = globals()["EXPECTED_SCHEMA_SHA256"]
        globals()["EXPECTED_SCHEMA_SHA256"] = sha256_file(bad)
        try:
            _read_verified_schema(bad)
        except CheckError:
            pass
        else:
            raise AssertionError("psql meta-command was accepted")
        finally:
            globals()["EXPECTED_SCHEMA_SHA256"] = original
    runner = FakeRunner()
    _restore_archive(runner, Path("/safe/fake.custom"))
    restore = " ".join(runner.calls[-1])
    for required in ("--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction", f"--dbname={RESTORE_DB}"):
        if required not in restore:
            raise AssertionError(f"restore command lacks {required}")
    if any("hermes " in " ".join(call) and "pg_restore" in " ".join(call) for call in runner.calls):
        raise AssertionError("restore command unexpectedly targets production")
    return "PASS: fake-runner guards, restore arguments, metadata comparison, and temp-only file checks"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-local-postgres-test", action="store_true")
    parser.add_argument("--schema-sql", type=Path, help="local exact schema.sql for optional test-DB rehearsal")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        print(_self_test())
        return 0
    if not args.execute_local_postgres_test:
        print("REFUSED: no PostgreSQL action. Use only after separate approval with --execute-local-postgres-test.")
        return 2
    try:
        print(execute(subprocess_runner, args.schema_sql))
        return 0
    except Inconclusive as error:
        print(f"INCONCLUSIVE: {error}")
        return 3
    except CheckError as error:
        print(f"STOPPED: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
