# Exact three-file future installer

`install_once.py` describes one guarded future root operation. Without
`--execute-exact-install` it refuses before constructing the real service adapter.
There is no configurable source or target path and no separate recovery command in
the current implementation.

The guarded path accepts only the three routes and hashes hard-coded in the script
and repeated in `../manifest.json`. It also requires the fixed protected staging
directory, the previously verified database dump and schema hashes, the exact target
ownership/modes and pre-image hashes, both application services healthy, both
oneshot jobs inactive, an empty and strictly attributed three-directory queue, and
the 12 September 2026 pre-06:30 Moscow start window. Source syntax is compiled with
the configured service interpreters without importing application modules.

The backup directory is fixed at
`/var/backups/hermes-files-20260912-4cb3`. It is created and all three backups are
validated before any service is stopped. A backup filesystem failure therefore does
not stop the services, although it can leave the new backup directory or partial
backup artifacts in place; the existing-directory guard then prevents a blind retry.
The installer records and fsyncs state before quiescing and before each replacement.
The coordinator added a parent-directory fsync after the review and reran all14
offline tests successfully. Abrupt power loss or process termination across three
separate replacements still requires inspection of the durable state and backups;
this suite does not prove automatic recovery from every hard-crash scenario.

Only a timer that was initially active is stopped and later restarted. After the
timer is paused, the installer drains the queue, stops the bot and worker, rechecks
all targets/backups, replaces exactly three files, and starts and observes the worker
and bot. A replacement or startup failure attempts the exact three-file rollback
only while the queue is still empty and both services can be quiesced. If work is
pending after startup, it deliberately does not stop the worker or overwrite files;
it reports an unsafe state for manual review and leaves the initially active timer
stopped. An initially inactive timer remains inactive.

Starting the bot inherently runs its already-approved startup schema behavior and
Telegram menu update. The installer does not call either operation directly and
cannot make those startup effects read-only. It does not directly perform a database
restore, daemon reload, force-stop, JSON move, or Telegram/model call. Its direct
mutations are limited to the three target files, fixed backup directory/state files,
and named units; the application-managed startup effects above remain indirect side
effects rather than a read-only operation.

`test_install_once.py` is an offline local test suite. Flow tests use an in-memory
service adapter and mocked release filesystem boundaries; focused tests use temporary
local files for queue fail-closed behavior, manifest rejection, and the actual backup
validation-before-rollback ordering. They do not call `systemctl`, a real service,
the production paths, SSH, a database, or GitHub. Passing these mocks is not evidence
that production permissions, ownership, interpreters, unit behavior, timing, or the
owner functional test will pass.

The test/review work itself did not deploy. Subsequently the coordinator performed
one separately reviewed production installation, with independent read-only checks.
See EXECUTION_SCOPE.md for the exact hashes, transient unit result, backup paths and
remaining owner acceptance. Do not rerun the installer: its new-backup guard will
refuse, and a repeat is not the post-deployment monitoring procedure.
