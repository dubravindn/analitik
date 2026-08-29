import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ЦБД — управленческий дашборд",
  description: "Закрытая аналитика Цветочной Базы Дубравиных.",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ru"><body>{children}</body></html>;
}
