import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const dashboardSnapshots = sqliteTable("dashboard_snapshots", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  periodFrom: text("period_from").notNull(),
  periodTo: text("period_to").notNull(),
  generatedAt: text("generated_at").notNull(),
  payload: text("payload").notNull(),
  createdAt: text("created_at").notNull().default("CURRENT_TIMESTAMP"),
});
