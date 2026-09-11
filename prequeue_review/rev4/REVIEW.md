# Prequeue candidate, revision 4 — NOT DEPLOYED

This is a reviewed patch artifact, not a complete installation package or an approval to deploy. It targets exact baselines, not necessarily the repository HEAD. Do not run the earlier three-file installer again.

## Independent local checks

- Synthetic bot/worker/renderer pipeline: PASS; external calls: 0.
- Fixed four-target local manifest validation: PASS; production calls/writes: 0.
- git apply --check against the exact baseline: PASS.
- Both tests were repeated by the coordinator on 12 September 2026.

The synthetic fixtures and exact private baseline copies remain in the project workspace; they are not included here. This artifact alone is not a self-contained test suite.

## Scope and limitations

Preparation uses a fresh subprocess with a deadline, bounded JSON and duplicate/capacity guards. It reads cached data in a read-only transaction instead of starting live ETL for every question. Only a request classified exactly as sales can proceed. Local day/store coverage and a successful sync record are checked; remote-source completeness is still unknown and must be disclosed in the final answer.

Product-level sales facts are deliberately excluded: their separate source coverage is not checked. Classification as sales is not proof that every requested detail is supported. Unsupported-domain requests are refused; arbitrary conversation and the full original business scope are not accepted as complete. Daily PDF generation is unchanged.

The model/runtime failure seen in the existing live question is not diagnosed or repaired by this patch. Enqueue I/O after preparation has no new deadline. Exact service/runtime/schema compatibility, backup/restore, addressed deployment approval and private/topic live acceptance remain outstanding.

## Integrity

Patch SHA256: 286027b2aa901d5634bbfbba155d71858a579c2ee26219442b892bd1e0c15f71

| File | Before SHA256 | Candidate SHA256 |
| --- | --- | --- |
| bot.py | cdf455c82fe455e7cf2d6486a133ded43970010339e35d94a93a590b0dd12b0d | 3e74a2affcc415b5952c24934badb84144ba84549d55718a17811cd1f5ff8269 |
| ai_worker.py | 6f3acadeebdd54f7a703c61d5298f2085fef4683fbaf2f13eb5a31f9f8690171 | 80ae72ee2d3f276376a3533b21c286021ae841920734be4744fa1fd025985958 |
| prequeue_guard.py | must be absent | 47d7e1b87d7bb39803037f4f5300734a53d7609a1b64d013fc54801fea61e74b |
| prequeue_runner.py | must be absent | a3585dc44d696453f3bd0229aa5742682625803d400cfd11a4ee966bda84ade3 |
