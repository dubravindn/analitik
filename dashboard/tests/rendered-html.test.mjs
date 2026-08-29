import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("contains the management dashboard and live-data boundary", async () => {
  const [page, layout] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(layout, /ЦБД — управленческий дашборд/i);
  assert.match(page, /Картина бизнеса/);
  assert.match(page, /Только чтение/);
  assert.match(page, /Контрольный макет/);
  assert.match(page, /getDashboardSnapshot/);
  assert.match(page, /Ресторанные склады исключены/);
  assert.doesNotMatch(page, /react-loading-skeleton|codex-preview/i);
});

test("protects the sync endpoint with a bearer token", async () => {
  const route = await readFile(
    new URL("../app/api/sync/route.ts", import.meta.url), "utf8",
  );
  assert.match(route, /DASHBOARD_SYNC_TOKEN/);
  assert.match(route, /Bearer \$\{expected\}/);
  assert.match(route, /status: 401/);
  assert.match(route, /invalid_snapshot/);
});
