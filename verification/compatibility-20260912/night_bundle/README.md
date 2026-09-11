# Night bundle: three-file offline verification package

**Purpose:** a reproducible local package of exactly three approved *target* Python files. It is evidence and an offline algorithm test, **not** a production installer or deployment instruction.

## Included target files

| ID | Package file | Future production target | SHA-256 (source = package = target) |
| --- | --- | --- | --- |
| `worker_ai_worker` | `files/worker/hermes/ai_worker.py` | `/var/lib/hermes-ai/app/hermes/ai_worker.py` | `6f3acadeebdd54f7a703c61d5298f2085fef4683fbaf2f13eb5a31f9f8690171` |
| `worker_ai_analyst` | `files/worker/hermes/ai_analyst.py` | `/var/lib/hermes-ai/app/hermes/ai_analyst.py` | `1bff538fb61a116a650c5210d22f125b2bfd993ffb62be74e2e0c3a75559560b` |
| `renderer_ai_analyst` | `files/renderer/hermes/ai_analyst.py` | `/opt/hermes/app/hermes/ai_analyst.py` | `8815da3bc31a4bb4c9a398c7c03b73f3c73daae319ab77bb9eac272bfc6ef41d` |

The two files named `ai_analyst.py` are deliberately different artifacts: the worker version is 29,575 bytes and belongs only under `/var/lib/hermes-ai`; the renderer version is 31,630 bytes and belongs only under `/opt/hermes`. Their unequal hashes are enforced by the verifier.

`manifest.json` records the complete local source location, the expected pre-replacement SHA-256, and the package/production target SHA-256 for each file. The expected old hashes are evidence from the accepted compatibility checks; they must be re-read from the real targets immediately before any separately authorized maintenance window.

## Offline checks

Run from this directory:

```sh
python3 -B verify_night_bundle.py verify
python3 -B verify_night_bundle.py self-test
```

`verify` verifies the manifest, the three package bytes, their distinct destinations, SHA-256 values, and syntax by compiling source text in memory. It never imports or executes a bundled module, creates no `.pyc`, and makes no network, database, Telegram, model, service, or production-filesystem call.

`self-test` creates its own temporary synthetic tree only. It demonstrates that the local algorithm:

1. preflights all three expected old hashes before writing;
2. replaces exactly the three synthetic target files and leaves a sentinel unchanged;
3. refuses a mismatched source before any synthetic target is changed;
4. restores the prior bytes and POSIX mode bits from a valid local backup; and
5. refuses restoration when a synthetic backup has been corrupted.

The test has no CLI path option and contains an explicit guard that allows writes only in a new direct child of `/private/tmp`; it cannot write `/opt`, `/var/lib`, or another supplied production path. It intentionally does not preserve or assert ownership, groups, ACLs, extended attributes, active queues, services, or real backup storage.

## Boundary and remaining production work

The B04 and renderer READMEs establish these as isolated compatibility artifacts, not a deployment. The addressable plan remains authoritative: production work would still need an explicit maintenance-window authorization, fresh read-only SHA/status/queue checks, timer and bot-start side-effect decisions, a real addressed backup and manifest, and a separate approved rollback procedure.

**Passed here:** local package integrity, syntax-only parsing, and synthetic restoration algorithm.

**Not performed here:** any production backup, production replacement, production restoration, server access, SSH, services, timers, or writes beneath `/opt` or `/var/lib`.
