# Future-only Hermes PostgreSQL backup and isolated restore check

`backup_restore_check.py` is an explicitly gated procedure, not an authorization to run it. Without `--execute-local-postgres-test` it refuses before creating a subprocess. Its initial preparation was offline. One separately reviewed server execution and guarded cleanup have since been performed; see the factual execution record below. Do not rerun against existing artifacts.

## Immutable scope

- Source database: `hermes`; it is used only for metadata reads and a custom-format `pg_dump`.
- New backup directory: `/var/backups/hermes-night-20260912-4cb3`, which must not already exist. Real execution requires root, confirms `/var/backups` is a real root-owned non-symlink directory, then exclusively uses `os.mkdir` for the exact new path and `chown`s it to `postgres:0700`. It never uses recursive creation or overwrites. The custom archive, SHA-256 manifest, and archive TOC are created under umask `077`, explicitly chmodded, and verified as `postgres:0600`.
- Only temporary restore database: `hermes_restorecheck_20260912_4cb3`. Its prior existence is a hard stop. The script never uses `DROP ... FORCE`, `IF EXISTS`, a wildcard, or `--clean`; it can delete only this named database after checking its exact comment marker through `shobj_description(oid, 'pg_database')` (database comments are shared-catalog descriptions) and that it has zero other connections.
- PostgreSQL clients run only through `sudo -n -u postgres`, using the local Unix socket `/var/run/postgresql`, a minimal environment without `PG*` values, argument arrays (`shell=False`), and timeouts.

## Future sequence

1. Check `postgres` identity, PostgreSQL major version 16, source size, free space (`>= 3 × source + 1 GiB`), and absence of the exact test database.
2. Record source metadata without printing values: table/index counts and sorted names, columns/defaults/types, index definitions, view definitions, the two required view fingerprints, and `public.holiday` count plus an order-independent row-set MD5.
3. Create a full `pg_dump --format=custom --lock-wait-timeout=10s hermes`, SHA-256 it, and validate it using `pg_restore --list`.
4. Create only the named temporary database from `template0` with connection limit zero, verify its encoding/locale metadata equals source, immediately `REVOKE CONNECT ... FROM PUBLIC`, attach the exact non-secret comment marker, then restore with `--no-owner --no-privileges --exit-on-error --single-transaction`.
5. Compare the restored metadata to the original, then compare source metadata once more. Any source drift or semantic mismatch is **inconclusive**: retain both dump and temporary database and report only the changed metadata category, never rows or definitions.
6. Optional rehearsal is enabled only by adding `--schema-sql /opt/hermes/app/hermes/schema.sql`. Before any database write the script reads its bytes once, verifies SHA-256 `a27afe9dae79a188de93f0bbd9a970d911e175a86a149ee29bd43fd48690e7ad`, and rejects psql metacommands or explicit transaction control. The verified in-memory text—not its path—is sent via `psql --file=-` only to the temporary database, under `--single-transaction` with local lock and statement timeouts. Any metadata difference after rehearsal is inconclusive and retains the evidence; it does not make deployment ready.
7. Only after all comparisons pass, verify the exact comment marker and zero other connections, then issue unforced `DROP DATABASE` for the named temporary database. The dump remains.

The custom archive is trusted only because its source is the owned `hermes` database. PostgreSQL warns that restoring an archive can execute code chosen by source superusers; a different or untrusted source must not use this procedure. [pg_dump documentation](https://www.postgresql.org/docs/16/app-pgdump.html) and [pg_restore documentation](https://www.postgresql.org/docs/16/app-pgrestore.html) describe that risk and the archive/restore flags. `CREATE DATABASE ... TEMPLATE template0` creates a pristine database; `DROP DATABASE` is irreversible and therefore remains guarded by the marker check. [CREATE DATABASE](https://www.postgresql.org/docs/16/sql-createdatabase.html), [DROP DATABASE](https://www.postgresql.org/docs/16/sql-dropdatabase.html), [COMMENT](https://www.postgresql.org/docs/16/sql-comment.html).

## Local checks performed now

```sh
python3 -B backup_restore_check.py
python3 -B backup_restore_check.py --self-test
```

The first command must refuse. The self-test uses only a fake runner and a fresh `/private/tmp/night-backup-test-*` directory; it invokes neither `sudo` nor PostgreSQL.

## Deliberate remaining risks and future side effects

The action creates a compressed dump and a temporary database, takes shared locks for `pg_dump` (which can fail on the configured timeout), executes trusted source archive code during restore, and executes the approved `schema.sql` only inside the temporary database. It may consume disk and PostgreSQL resources. It does not validate external/off-host backup, preserve the temporary DB on a clean pass, or restore production `hermes`; a logical dump is not a safe automated rollback over later production writes.

## Actual execution: 12 September 2026, approximately 01:15–01:17 MSK

The coordinator ran the exact procedure over the existing SSH connection after normal tool risk review accepted this bounded action. Script SHA-256 at that time: `bde634ffdd238dccdb8e41acdd70904aa5478320ab7d3edb1a97b13e16d3f568`.

- Full custom-format archive created: `/var/backups/hermes-night-20260912-4cb3/hermes.custom`, 11,275,698 bytes, SHA-256 `21d1b469807d2353c7bd2ba6335a547a7d4839432c60ad149a6b211bf16c9551`. Archive, checksum and TOC were independently checked as postgres-owned mode0600. Checksum matched.
- Actual restore into the new named test database completed. Source metadata before/after, restored metadata, and metadata after applying the exact startup SQL compared equal. This covers the fields in `collect_metadata`, not all business facts or all possible database semantics.
- Initial execution stopped safely at cleanup, exit3: the original marker query incorrectly used `obj_description` for the shared database catalog. Read-only investigation confirmed the correct marker through `shobj_description`, owner postgres, connection limit0, and zero connections. The local script and a regression test were corrected; 15 offline tests passed.
- A separate reviewed cleanup rechecked the exact archive hash, marker, owner, connection limit and zero connections, then deleted only `hermes_restorecheck_20260912_4cb3`. Its absence was confirmed. The archive remains and can recreate the test copy. The updated full script was not rerun: existing backup paths deliberately cause refusal.
- No production restore, production schema write, application restart, Telegram request or model request was performed by these steps. This is a verified backup/isolated rehearsal, not a completed deployment or an off-server disaster-recovery backup.
