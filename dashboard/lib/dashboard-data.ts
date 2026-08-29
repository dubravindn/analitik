import { desc } from "drizzle-orm";
import { getDb } from "../db";
import { dashboardSnapshots } from "../db/schema";

export type DashboardMetric = {
  revenueKop: number;
  grossProfitKop: number;
  profitBeforeWriteoffsKop: number;
  profitAfterWriteoffsKop: number;
  writeoffsKop: number;
  operatingExpensesKop: number;
  checks: number;
};

export type DashboardStore = {
  name: string;
  revenueKop: number;
};

export type DashboardSnapshot = {
  schemaVersion: 1;
  generatedAt: string;
  period: { from: string; to: string; label: string };
  metrics: DashboardMetric;
  stores: DashboardStore[];
  comparison: {
    label: string;
    period: { from: string; to: string };
    metrics: DashboardMetric;
  } | null;
  clients?: {
    summary: { clients: number; orders: number; revenueKop: number };
    top: Array<{ name: string; orders: number; revenueKop: number }>;
    churn: Array<{
      name: string;
      lastOrder: string;
      daysSince: number;
      orders: number;
      revenueKop: number;
      averageCheckKop: number;
    }>;
  };
  liveB2b?: {
    available: boolean;
    asOf?: string;
    error?: string;
    receivables?: {
      totalKop: number;
      clients: number;
      rule: string;
      rows: Array<{
        name: string;
        balanceKop: number;
        lastDemandDate: string;
        demandsCount: number;
        demandsSumKop: number;
      }>;
    };
    openOrders?: {
      orders: number;
      sumKop: number;
      remainingToShipKop: number;
      unpaidKop: number;
      rows: Array<{
        number: string;
        moment: string;
        client: string;
        state: string;
        sumKop: number;
        remainingToShipKop: number;
        unpaidKop: number;
        reservedKop: number;
      }>;
    };
  };
  aiSummary?: {
    available: boolean;
    runId?: string;
    reportType?: string;
    reportId?: string;
    completedAt?: string;
    findings?: Array<{
      severity: string;
      title: string;
      evidence: string;
      whyItMatters: string;
      action: string;
    }>;
    warnings?: string[];
  };
};

export const CONTROL_SNAPSHOT: DashboardSnapshot = {
  schemaVersion: 1,
  generatedAt: "2026-08-23T00:00:00+03:00",
  period: { from: "2026-08-16", to: "2026-08-22", label: "16–22 августа 2026" },
  metrics: {
    revenueKop: 255576644,
    grossProfitKop: 87328564,
    profitBeforeWriteoffsKop: 60712164,
    profitAfterWriteoffsKop: 55069346,
    writeoffsKop: 5642818,
    operatingExpensesKop: 26616400,
    checks: 601,
  },
  stores: [
    { name: "База Воровского 107/1", revenueKop: 190125230 },
    { name: "Киров, Ленина 102А", revenueKop: 23966649 },
    { name: "Розница Воровского 107/1", revenueKop: 22014899 },
    { name: "Слободской, Советская 64", revenueKop: 19469866 },
  ],
  comparison: {
    label: "Предыдущий период такой же длины",
    period: { from: "2026-08-09", to: "2026-08-15" },
    metrics: {
      revenueKop: 275964883,
      grossProfitKop: 90954898,
      profitBeforeWriteoffsKop: 68493698,
      profitAfterWriteoffsKop: 62951546,
      writeoffsKop: 5542152,
      operatingExpensesKop: 22461200,
      checks: 579,
    },
  },
  clients: undefined,
  liveB2b: undefined,
  aiSummary: undefined,
};

export async function getDashboardSnapshot(): Promise<{
  snapshot: DashboardSnapshot;
  isLive: boolean;
}> {
  try {
    const [row] = await getDb()
      .select({ payload: dashboardSnapshots.payload })
      .from(dashboardSnapshots)
      .orderBy(desc(dashboardSnapshots.id))
      .limit(1);
    if (!row) return { snapshot: CONTROL_SNAPSHOT, isLive: false };
    return { snapshot: JSON.parse(row.payload) as DashboardSnapshot, isLive: true };
  } catch {
    return { snapshot: CONTROL_SNAPSHOT, isLive: false };
  }
}

export function formatRubles(kop: number): string {
  return `${Math.round(kop / 100).toLocaleString("ru-RU")} ₽`;
}

export function formatDelta(current: number, previous: number): string {
  if (!previous) return "—";
  const value = (current - previous) / Math.abs(previous) * 100;
  return `${value >= 0 ? "+" : ""}${Math.round(value)}%`;
}
