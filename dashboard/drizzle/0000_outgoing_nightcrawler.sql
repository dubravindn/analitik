CREATE TABLE `dashboard_snapshots` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`period_from` text NOT NULL,
	`period_to` text NOT NULL,
	`generated_at` text NOT NULL,
	`payload` text NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
