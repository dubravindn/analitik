import { env } from "cloudflare:workers";
import { desc, lt } from "drizzle-orm";
import { getDb } from "../../../db";
import { dashboardSnapshots } from "../../../db/schema";
import type { DashboardSnapshot } from "../../../lib/dashboard-data";

export const dynamic = "force-dynamic";

function syncToken(): string | null {
  const value = (env as unknown as Record<string, unknown>).DASHBOARD_SYNC_TOKEN;
  return typeof value === "string" && value ? value : null;
}

async function ensureSnapshotTable(): Promise<void> {
  const database = (env as unknown as Record<string, unknown>).DB as
    | { exec: (query: string) => Promise<unknown> }
    | undefined;
  if (!database) throw new Error("D1 binding DB is unavailable");
  await database.exec(
    "CREATE TABLE IF NOT EXISTS dashboard_snapshots (" +
    "id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL," +
    "period_from TEXT NOT NULL," +
    "period_to TEXT NOT NULL," +
    "generated_at TEXT NOT NULL," +
    "payload TEXT NOT NULL," +
    "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
  );
}

function isSnapshot(value: unknown): value is DashboardSnapshot {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<DashboardSnapshot>;
  return candidate.schemaVersion === 1 && Boolean(candidate.period) &&
    Boolean(candidate.metrics) && Array.isArray(candidate.stores) &&
    typeof candidate.generatedAt === "string";
}

export async function POST(request: Request) {
  const expected = syncToken();
  const actual = request.headers.get("authorization");
  if (!expected || actual !== `Bearer ${expected}`) {
    return Response.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  let snapshot: unknown;
  try {
    snapshot = await request.json();
  } catch {
    return Response.json({ ok: false, error: "invalid_json" }, { status: 400 });
  }
  if (!isSnapshot(snapshot)) {
    return Response.json({ ok: false, error: "invalid_snapshot" }, { status: 422 });
  }

  await ensureSnapshotTable();
  const db = getDb();
  const [inserted] = await db.insert(dashboardSnapshots).values({
    periodFrom: snapshot.period.from,
    periodTo: snapshot.period.to,
    generatedAt: snapshot.generatedAt,
    payload: JSON.stringify(snapshot),
  }).returning({ id: dashboardSnapshots.id });

  const [keepFrom] = await db.select({ id: dashboardSnapshots.id })
    .from(dashboardSnapshots)
    .orderBy(desc(dashboardSnapshots.id))
    .limit(1)
    .offset(89);
  if (keepFrom?.id) {
    await db.delete(dashboardSnapshots).where(lt(dashboardSnapshots.id, keepFrom.id));
  }

  return Response.json({ ok: true, id: inserted.id });
}
