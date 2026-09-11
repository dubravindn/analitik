# Analyst compatibility evidence — 2026-09-12

This branch preserves offline patches and reproducible regression checks. **No application files outside this verification directory are changed. No production deployment is performed or implied.** This is not a ready-to-deploy release.

## Two different source versions

- Worker baseline: upstream commit `d3628aa3e6bc2d73323425031488901e344885b5`. The worker's `ai_analyst.py` baseline SHA-256 is `8675efa855c284c8635a42d766fe8e64650b59834c270713e7286fc5bde05e61`.
- The bot's separate renderer baseline SHA-256 is `9f837ec597b109329d8caf9477881a01e6cec791eef8130354950588385c3242`, from the local `b5c1fc688758b4e435f5284304e546e372ab5e40` source version. A read-only production inventory previously found the same hash. Recheck immediately before any later deployment.
- Patched bot renderer SHA-256: `8815da3bc31a4bb4c9a398c7c03b73f3c73daae319ab77bb9eac272bfc6ef41d`.

The worker and the bot renderer must not be overwritten with each other's full source file. Their runtime directories and versions differ.

## Contents

- `b04/proposed_full.patch`: preserve delivery metadata and source-refresh state; include bounded product-stock facts and explicit truncation; worker-version rendering change.
- `renderer/renderer.patch`: separately rebased freshness-warning change for the exact bot renderer.
- `full_source`, `patched_full`, `baseline`, `patched`: isolated snapshots for tests, not installation directories.
- `telegram.py` in the B04 snapshots is a fake sender that records calls. It is not the real Telegram client and MUST NOT be deployed.

## Run offline

Use Python 3.10 or later from the repository root:

```sh
python3 -B verification/compatibility-20260912/b04/test_full_path.py
python3 -B verification/compatibility-20260912/renderer/test_renderer.py
git -C verification/compatibility-20260912/b04/full_source apply --check --no-index ../proposed_full.patch
git -C verification/compatibility-20260912/renderer/baseline apply --check --no-index ../renderer.patch
```

The tests use synthetic facts and fake database/model/Telegram functions. They do not read credentials, invoke production ETL, contact a model, or send real messages. Temporary files are isolated.

Verified boundaries: exact baseline hash; only the intended bot-rendering function changes; no-error text stays identical; freshness warning is mandatory when refresh errors exist; private-chat/forum-topic delivery and worker success/failure are checked through fake delivery. The B04 test also checks bounded context: 30 product-stock facts plus 400 financial facts, with explicit omission counts.

## Not yet verified / deployment gate

These tests do not run two production processes, validate financial accounting, or demonstrate real Telegram delivery. Before any deployment, finish the producer/timer drain procedure, account for bot startup side effects (schema application and Telegram command registration), test backup/restore and stalled-queue recovery, and obtain approval for the exact deployment plan. Do not apply this branch automatically or install the test snapshots.

No secrets, account credentials, database extracts, raw customer messages, reports, or project-wide personal documents are intentionally included.
