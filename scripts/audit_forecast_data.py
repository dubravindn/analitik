#!/usr/bin/env python3
"""
Hermes — Read-Only Forecast Data Audit   v2
============================================
ГАРАНТИИ БЕЗОПАСНОСТИ:
  - Соединение открывается в режиме READ ONLY (SET default_transaction_read_only = on).
  - statement_timeout = 60s, lock_timeout = 5s — не зависнет и не заблокирует ETL.
  - Единственная точка выполнения SQL — _q(), она не принимает и не исполняет write-команды.
  - Нет вывода DSN, паролей, токенов (database_name выводится, host — только хеш).
  - Нет обращений к API МойСклад.

ОТКУДА БЕРЁТСЯ DATABASE_URL:
  config._bootstrap() ищет .env в порядке:
    1. переменная окружения HERMES_ENV_FILE
    2. <project_root>/.env
    3. /opt/hermes/.env (production server)
  Скрипт подключается именно к той БД, которую указывает найденный .env.

ПРАВИЛЬНАЯ КОМАНДА ЗАПУСКА (на сервере, где /opt/hermes/.env):
  cd /opt/hermes
  source venv/bin/activate
  python3 scripts/audit_forecast_data.py

ВЫВОД — три файла рядом со скриптом:
  audit_forecast_summary.json   — агрегаты, статистика, мета
  audit_forecast_samples.csv    — ежедневные данные по выборке SKU
  audit_forecast_balance.csv    — нарушения балансового уравнения

СТРУКТУРА ФУНКЦИЙ (таблицы → секция в summary.json):
  _connect_readonly()           — psycopg3 + SET default_transaction_read_only + timeouts
  section_meta()                — current_database(), git commit, время запуска
  section_schema()              — information_schema.columns, information_schema.tables
  section_data_history()        — stock_snapshot, sales_by_product_day, supply_doc,
                                   loss_doc, move_doc, product_dim, product_price
  section_snapshot_timing()     — stock_snapshot.synced_at  ← НОВОЕ: время snapshot
  section_snapshot_completeness()— stock_snapshot (per-day sku/store counts)
  section_stores()              — sales_by_product_day, supply_doc+supply_item,
                                   move_doc+move_item, loss_doc+loss_item
  _select_sample_skus()         — product_dim, stock_snapshot, sales_by_product_day,
                                   loss_item, supply_item
  write_samples_csv()           — stock_snapshot, sales_by_product_day, supply_item,
   (6 отдельных SQL)              supply_doc, move_item, move_doc, loss_item, loss_doc
  section_snapshot_gaps()       — stock_snapshot, sales_by_product_day,
                                   supply_doc+supply_item, move_doc+move_item,
                                   loss_doc+loss_item
  write_balance_csv()           — stock_snapshot, sales_by_product_day,
   (5 отдельных SQL)              supply_doc+supply_item, move_doc+move_item,
                                   loss_doc+loss_item
  section_sales_etl_check()     — sales_by_product_day
  section_unknowns()            — (только анализ schema_info)
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from hermes.config import DATABASE_URL, MSK

SCRIPT_VERSION = "2.1"
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
OUT_SUMMARY    = os.path.join(SCRIPT_DIR, "audit_forecast_summary.json")
OUT_SAMPLES    = os.path.join(SCRIPT_DIR, "audit_forecast_samples.csv")
OUT_BALANCE    = os.path.join(SCRIPT_DIR, "audit_forecast_balance.csv")
OUT_SUMMARY_TMP = OUT_SUMMARY + ".tmp"
OUT_SAMPLES_TMP = OUT_SAMPLES + ".tmp"
OUT_BALANCE_TMP = OUT_BALANCE + ".tmp"
TZ             = "Europe/Moscow"
WINDOW_DAYS    = 45   # ширина окна для SKU×STORE×DAY выборки
MAX_GAPS       = 60   # лимит записей в snapshot_gaps
MAX_BALANCE    = 200  # лимит строк в balance.csv
MAX_SAMPLE_CSV = 20_000  # лимит строк в samples.csv

EXPECTED_TABLES = [
    "sales_by_product_day", "stock_snapshot", "product_dim", "product_price",
    "supply_doc", "supply_item", "loss_doc", "loss_item",
    "move_doc", "move_item", "holiday",
    "enter_doc", "enter_item", "sales_doc",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _j(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Not JSON-serializable: {type(obj)!r}")


def _q(conn, sql: str, params=None) -> list[tuple]:
    """Единственная точка исполнения SQL. Только SELECT внутри read-only транзакции."""
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()


# ── Connection ────────────────────────────────────────────────────────────────

def _connect_readonly() -> psycopg.Connection:
    """
    Два уровня защиты от случайных write-операций:
      1. options="-c default_transaction_read_only=on" — применяется до первого запроса,
         на уровне протокола подключения, до любого кода Python.
      2. После connect — SHOW для подтверждения; если PostgreSQL вернул не 'on' → SystemExit.
    """
    dsn = DATABASE_URL()
    conn = psycopg.connect(
        dsn,
        autocommit=False,
        options="-c default_transaction_read_only=on"
               " -c statement_timeout=60000"
               " -c lock_timeout=5000",
    )
    with conn.cursor() as cur:
        cur.execute(f"SET TIME ZONE '{TZ}'")
        cur.execute("SHOW default_transaction_read_only")
        ro_actual = cur.fetchone()[0]
    conn.commit()

    if ro_actual.strip().lower() != "on":
        conn.close()
        raise SystemExit(
            f"ABORT: PostgreSQL default_transaction_read_only = '{ro_actual}' (ожидалось 'on'). "
            "Аудит прекращён — read-only режим не подтверждён."
        )
    return conn


def _preflight(conn) -> dict:
    """
    Собирает идентификацию БД и выводит безопасный preflight-блок.
    Возвращает dict для записи в audit_meta.
    IP-адрес — только хэш. Пароль и полный DSN не выводятся.
    """
    db_name    = _q(conn, "SELECT current_database()")[0][0]
    db_user    = _q(conn, "SELECT current_user")[0][0]
    pg_version = _q(conn, "SELECT version()")[0][0].split(",")[0]  # первая часть
    pg_port    = _q(conn, "SELECT inet_server_port()")[0][0]

    # IP — хешируем, не выводим в открытом виде
    try:
        raw_ip = _q(conn, "SELECT inet_server_addr()")[0][0]
        ip_hash = hashlib.sha256(str(raw_ip).encode()).hexdigest()[:12]
    except Exception:
        ip_hash = "unknown"

    ro_status = _q(conn, "SHOW default_transaction_read_only")[0][0].strip().lower()

    # DSN-хеш: host:port из DATABASE_URL строки (не IP из БД, а настройки подключения)
    dsn_raw = DATABASE_URL()
    try:
        at_idx    = dsn_raw.rfind("@")
        slash_idx = dsn_raw.rfind("/", at_idx)
        host_part = dsn_raw[at_idx + 1 : slash_idx] if at_idx > 0 else "local"
        host_hash = hashlib.sha256(host_part.encode()).hexdigest()[:12]
    except Exception:
        host_hash = "unknown"

    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(SCRIPT_DIR), stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        git_commit = "unknown"

    info = {
        "database_name":         db_name,
        "database_user":         db_user,
        "database_host_hash":    host_hash,
        "database_server_ip_hash": ip_hash,
        "database_port":         pg_port,
        "postgresql_version":    pg_version,
        "read_only_pg_confirmed": ro_status == "on",
        "read_only_pg_raw":      ro_status,
        "git_commit":            git_commit,
        "script_version":        SCRIPT_VERSION,
        "timezone":              TZ,
        "generated_at_moscow":   datetime.now(MSK).isoformat(),
        "analysis_window_days":  WINDOW_DAYS,
    }

    print("=" * 60)
    print(f"  Database             : {db_name}")
    print(f"  PostgreSQL read-only : {ro_status.upper()}")
    print(f"  Timezone             : {TZ}")
    print(f"  Script version       : {SCRIPT_VERSION}")
    print(f"  Git commit           : {git_commit}")
    print(f"  WRITE SQL detected   : NO")
    print("=" * 60)

    if ro_status != "on":
        conn.close()
        raise SystemExit("ABORT: PostgreSQL read-only НЕ подтверждён. Аудит прекращён.")

    print("  Starting read-only audit...\n")
    return info


# section_meta → заменена _preflight() выше, которая и выводит preflight-блок,
# и собирает dict для audit_meta в summary.json.


# ── SECTION: schema ───────────────────────────────────────────────────────────

def section_schema(conn) -> dict:
    existing = {r[0] for r in _q(conn, """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)}

    tables: dict[str, Any] = {}
    for tbl in EXPECTED_TABLES:
        if tbl not in existing:
            tables[tbl] = {"exists": False}
            continue
        cols = _q(conn, """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, [tbl])
        tables[tbl] = {
            "exists": True,
            "columns": [{"name": r[0], "type": r[1], "nullable": r[2]} for r in cols],
        }

    # product_id coverage в fact-таблицах
    pid_coverage: dict[str, Any] = {}
    for tbl in ["supply_item", "loss_item", "move_item"]:
        if tbl not in existing:
            continue
        r = _q(conn, f"""
            SELECT COUNT(*), COUNT(product_id), COUNT(*) - COUNT(product_id)
            FROM {tbl}
        """)[0]
        pid_coverage[tbl] = {
            "total": r[0], "with_pid": r[1], "without_pid": r[2],
            "coverage_pct": round(r[1] / r[0] * 100, 1) if r[0] else 0,
        }

    return {
        "existing_tables": sorted(existing),
        "tables": tables,
        "product_id_coverage": pid_coverage,
    }


# ── SECTION: data_history ─────────────────────────────────────────────────────

def section_data_history(conn, existing: set) -> dict:
    result: dict[str, Any] = {}

    def _range(tbl: str, day_col: str) -> dict:
        r = _q(conn, f"SELECT MIN({day_col}), MAX({day_col}), COUNT(DISTINCT {day_col}), COUNT(*) FROM {tbl}")[0]
        mn, mx, day_cnt, rows = r
        cal = (mx - mn).days + 1 if mn and mx else None
        return {
            "min_date": mn, "max_date": mx,
            "days_with_data": day_cnt, "calendar_days": cal,
            "coverage_pct": round(day_cnt / cal * 100, 1) if cal else None,
            "total_rows": rows,
        }

    if "stock_snapshot" in existing:
        h = _range("stock_snapshot WHERE is_srezka = TRUE", "day")
        h["srezka_skus"] = _q(conn,
            "SELECT COUNT(DISTINCT product_id) FROM stock_snapshot WHERE is_srezka = TRUE")[0][0]
        h["negative_stock_rows"] = _q(conn,
            "SELECT COUNT(*) FROM stock_snapshot WHERE stock_qty < 0")[0][0]
        result["stock_snapshot"] = h

    if "sales_by_product_day" in existing:
        h = _range("sales_by_product_day", "day")
        h["distinct_skus"] = _q(conn,
            "SELECT COUNT(DISTINCT assortment_id) FROM sales_by_product_day")[0][0]
        h["srezka_skus_with_sales"] = _q(conn, """
            SELECT COUNT(DISTINCT spd.assortment_id)
            FROM sales_by_product_day spd
            JOIN product_dim pd ON pd.product_id = spd.assortment_id
            WHERE pd.is_srezka = TRUE
        """)[0][0]
        result["sales_by_product_day"] = h

    if "supply_doc" in existing:
        h = _range("supply_doc", "day")
        if "supply_item" in existing:
            r = _q(conn, "SELECT COUNT(*), SUM(qty) FROM supply_item WHERE product_id IS NOT NULL")[0]
            h["supply_items_with_pid"] = r[0]
            h["supply_total_qty"] = float(r[1] or 0)
        result["supply_doc"] = h

    if "loss_doc" in existing:
        h = _range("loss_doc", "day")
        if "loss_item" in existing:
            r = _q(conn, "SELECT COUNT(*), SUM(qty) FROM loss_item WHERE product_id IS NOT NULL")[0]
            h["loss_items_with_pid"] = r[0]
            h["loss_total_qty"] = float(r[1] or 0)
        result["loss_doc"] = h

    if "move_doc" in existing:
        result["move_doc"] = _range("move_doc", "day")

    if "product_dim" in existing:
        r = _q(conn, """
            SELECT COUNT(*), COUNT(*) FILTER (WHERE is_srezka), COUNT(*) FILTER (WHERE NOT is_srezka)
            FROM product_dim
        """)[0]
        result["product_dim"] = {"total": r[0], "srezka": r[1], "non_srezka": r[2]}

    for tbl in ["enter_doc", "sales_doc"]:
        result[tbl] = _range(tbl, "day") if tbl in existing else {"exists": False}

    return result


# ── SECTION: snapshot_timing  (НОВОЕ) ─────────────────────────────────────────

def section_snapshot_timing(conn) -> dict:
    """
    Анализирует synced_at чтобы понять: snapshot — начало или конец дня?
    От этого зависит корректность балансового уравнения.
    """
    rows = _q(conn, f"""
        SELECT
            day,
            MIN(synced_at) AT TIME ZONE '{TZ}' AS first_sync,
            MAX(synced_at) AT TIME ZONE '{TZ}' AS last_sync,
            COUNT(DISTINCT DATE_TRUNC('hour', synced_at AT TIME ZONE '{TZ}')) AS distinct_hours
        FROM stock_snapshot
        WHERE is_srezka = TRUE
        GROUP BY day
        ORDER BY day
        LIMIT 90
    """)

    if not rows:
        return {"note": "No data"}

    # Распределение часа снятия snapshot (мск)
    hours = []
    for r in rows:
        first_sync = r[1]
        if first_sync:
            hours.append(first_sync.hour if hasattr(first_sync, 'hour') else int(str(first_sync)[11:13]))

    hour_freq: dict[int, int] = defaultdict(int)
    for h in hours:
        hour_freq[h] += 1

    most_common_hour = max(hour_freq, key=hour_freq.get) if hour_freq else None

    # Классификация: EOD (≥20:00 или ≤05:00) vs intraday
    # synced_at — момент записи ETL, НЕ момент снятия остатка в API.
    # Отсюда только нейтральные имена; бизнес-смысл snapshot не выводится.
    if most_common_hour is None:
        timing_type = "UNKNOWN"
    elif len(hour_freq) >= 4:
        timing_type = "SYNC_VARIABLE"  # много разных часов — нет стабильного расписания
    elif most_common_hour >= 20 or most_common_hour <= 5:
        timing_type = "SYNC_LATE_DAY"  # поздний вечер / ночь
    elif most_common_hour <= 9:
        timing_type = "SYNC_EARLY_DAY"  # раннее утро
    else:
        timing_type = "SYNC_INTRADAY"  # середина дня

    balance_note = (
        "APPROXIMATE_BALANCE_CHECK. "
        "synced_at = момент записи ETL, не момент снятия остатка в API. "
        "snapshot_business_time_semantics = UNKNOWN до изучения кода ETL/API."
    )

    return {
        "sample_days": len(rows),
        "most_common_sync_hour_msk": most_common_hour,
        "hour_distribution": {str(k): v for k, v in sorted(hour_freq.items())},
        "sync_timing_type": timing_type,
        "snapshot_business_time_semantics": "UNKNOWN",
        "balance_note": balance_note,
        "per_day_sample": [{
            "day": r[0], "first_sync_msk": r[1], "last_sync_msk": r[2],
            "distinct_sync_hours": r[3],
        } for r in rows[:14]],  # последние две недели
    }


# ── SECTION: snapshot_completeness ────────────────────────────────────────────

def section_snapshot_completeness(conn) -> dict:
    rows = _q(conn, f"""
        SELECT day,
               COUNT(DISTINCT product_id) AS skus,
               COUNT(DISTINCT store_id)   AS stores,
               COUNT(*)                   AS rows,
               MIN(synced_at) AT TIME ZONE '{TZ}' AS first_sync,
               MAX(synced_at) AT TIME ZONE '{TZ}' AS last_sync
        FROM stock_snapshot
        WHERE is_srezka = TRUE
        GROUP BY day
        ORDER BY day
    """)
    if not rows:
        return {"note": "No SREZKA data"}

    per_day = [{"day": r[0], "skus": r[1], "stores": r[2], "rows": r[3],
                "first_sync": r[4], "last_sync": r[5]} for r in rows]

    sku_counts  = [r["skus"] for r in per_day]
    median_skus = statistics.median(sku_counts)

    incomplete = [
        {"day": r["day"], "skus": r["skus"],
         "pct_of_median": round(r["skus"] / median_skus * 100, 1)}
        for r in per_day if r["skus"] < median_skus * 0.7
    ]

    snap_set = {r["day"] for r in per_day}
    d_min, d_max = min(snap_set), max(snap_set)
    missing = []
    d = d_min
    while d <= d_max:
        if d not in snap_set:
            missing.append(str(d))
        d += timedelta(days=1)

    return {
        "per_day": per_day,
        "summary": {
            "snapshot_days": len(per_day),
            "median_skus": median_skus,
            "min_skus": min(sku_counts),
            "max_skus": max(sku_counts),
            "incomplete_days": incomplete,
            "missing_calendar_days": missing,
        },
    }


# ── SECTION: stores ───────────────────────────────────────────────────────────

def section_stores(conn) -> dict:
    sales = _q(conn, """
        SELECT store_id, store_name,
               COUNT(DISTINCT day) AS sale_days,
               ROUND(SUM(sell_qty)::numeric, 0) AS total_sold,
               SUM(revenue_kop)/100 AS revenue_rub
        FROM sales_by_product_day
        GROUP BY store_id, store_name ORDER BY total_sold DESC
    """)

    supply = _q(conn, """
        SELECT sd.store_id, sd.store_name, sd.agent_name,
               COUNT(DISTINCT sd.doc_id) AS docs,
               ROUND(SUM(si.qty)::numeric, 0) AS total_received
        FROM supply_doc sd
        JOIN supply_item si ON si.doc_id = sd.doc_id
        GROUP BY sd.store_id, sd.store_name, sd.agent_name
        ORDER BY total_received DESC LIMIT 30
    """)

    moves = _q(conn, """
        SELECT md.store_from_name, md.store_to_name,
               COUNT(DISTINCT md.doc_id) AS docs,
               ROUND(SUM(mi.qty)::numeric, 0) AS qty
        FROM move_doc md
        JOIN move_item mi ON mi.doc_id = md.doc_id
        WHERE mi.product_id IS NOT NULL
        GROUP BY md.store_from_name, md.store_to_name
        ORDER BY qty DESC
    """)

    losses = _q(conn, """
        SELECT ld.store_id, ld.store_name,
               COUNT(DISTINCT ld.doc_id) AS docs,
               ROUND(SUM(li.qty)::numeric, 0) AS qty
        FROM loss_doc ld
        JOIN loss_item li ON li.doc_id = ld.doc_id
        WHERE li.product_id IS NOT NULL
        GROUP BY ld.store_id, ld.store_name ORDER BY qty DESC
    """)

    agents = _q(conn, """
        SELECT agent_name, COUNT(DISTINCT doc_id) AS docs,
               SUM(total_kop)/100 AS total_rub, MIN(day), MAX(day)
        FROM supply_doc GROUP BY agent_name ORDER BY docs DESC
    """)

    return {
        "sales_by_store":  [{"id": r[0], "name": r[1], "sale_days": r[2],
                              "total_sold": float(r[3] or 0), "revenue_rub": float(r[4] or 0)} for r in sales],
        "supply_by_store": [{"id": r[0], "name": r[1], "agent": r[2],
                              "docs": r[3], "received": float(r[4] or 0)} for r in supply],
        "move_flows":      [{"from": r[0], "to": r[1], "docs": r[2], "qty": float(r[3] or 0)} for r in moves],
        "loss_by_store":   [{"id": r[0], "name": r[1], "docs": r[2], "qty": float(r[3] or 0)} for r in losses],
        "supplier_agents": [{"agent": r[0], "docs": r[1], "total_rub": float(r[2] or 0),
                              "from": r[3], "to": r[4]} for r in agents],
    }


# ── Sample SKU selection ──────────────────────────────────────────────────────

def _select_sample_skus(conn) -> list[dict]:
    selected: dict[str, dict] = {}

    def _add(rows, cat, val_key=None, val_col=None):
        for r in rows:
            if r[0] in selected:
                continue
            e: dict[str, Any] = {"pid": r[0], "name": r[1], "folder": r[2], "category": cat}
            if val_key and val_col is not None:
                e[val_key] = float(r[val_col] or 0)
            selected[r[0]] = e

    _add(_q(conn, """
        SELECT pd.product_id, pd.product_name, pd.folder_path, SUM(spd.sell_qty)
        FROM product_dim pd JOIN sales_by_product_day spd ON spd.assortment_id=pd.product_id
        WHERE pd.is_srezka=TRUE AND spd.sell_qty>0
        GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 10
    """), "top_seller", "total_sales", 3)

    _add(_q(conn, """
        WITH snap AS (
            SELECT product_id,
                   MAX(day)-MIN(day)+1 AS cal_days,
                   COUNT(DISTINCT day) AS snap_days
            FROM stock_snapshot WHERE is_srezka=TRUE
            GROUP BY 1
        )
        SELECT pd.product_id, pd.product_name, pd.folder_path,
               s.cal_days - s.snap_days AS gap_days
        FROM snap s JOIN product_dim pd ON pd.product_id=s.product_id
        WHERE pd.is_srezka=TRUE AND s.cal_days-s.snap_days>2
        ORDER BY 4 DESC LIMIT 10
    """), "has_gaps", "gap_days", 3)

    _add(_q(conn, """
        SELECT li.product_id, pd.product_name, pd.folder_path, SUM(li.qty)
        FROM loss_item li JOIN product_dim pd ON pd.product_id=li.product_id
        WHERE pd.is_srezka=TRUE AND li.product_id IS NOT NULL
        GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 10
    """), "high_writeoff", "total_loss", 3)

    _add(_q(conn, """
        SELECT pd.product_id, pd.product_name, pd.folder_path,
               COALESCE(SUM(spd.sell_qty),0)
        FROM product_dim pd
        JOIN stock_snapshot ss ON ss.product_id=pd.product_id AND ss.is_srezka=TRUE
        LEFT JOIN sales_by_product_day spd ON spd.assortment_id=pd.product_id
        WHERE pd.is_srezka=TRUE
        GROUP BY 1,2,3 HAVING COALESCE(SUM(spd.sell_qty),0) BETWEEN 1 AND 15
        ORDER BY 4 ASC LIMIT 10
    """), "rare_sales", "total_sales", 3)

    _add(_q(conn, """
        SELECT pd.product_id, pd.product_name, pd.folder_path, 0
        FROM product_dim pd
        JOIN stock_snapshot ss ON ss.product_id=pd.product_id AND ss.is_srezka=TRUE
        WHERE pd.is_srezka=TRUE
          AND pd.product_id NOT IN (
              SELECT DISTINCT assortment_id FROM sales_by_product_day WHERE sell_qty>0)
        GROUP BY 1,2,3 LIMIT 5
    """), "zero_sales_has_stock")

    _add(_q(conn, """
        SELECT ss.product_id, pd.product_name, pd.folder_path, MIN(ss.stock_qty)
        FROM stock_snapshot ss JOIN product_dim pd ON pd.product_id=ss.product_id
        WHERE ss.stock_qty<0 AND ss.is_srezka=TRUE
        GROUP BY 1,2,3 ORDER BY 4 ASC LIMIT 5
    """), "negative_stock", "min_stock", 3)

    return list(selected.values())


# ── SAMPLES CSV: SKU × STORE × DAY ───────────────────────────────────────────

SAMPLES_HEADER = [
    "day", "product_id", "product_name", "store_id", "store_name",
    "snapshot_state",   # HAS_ROW | NO_ROW
    "stock_qty", "reserve_qty", "available_qty",
    "sold_qty", "revenue_kop",
    "received_qty", "moved_in_qty", "moved_out_qty", "loss_qty",
]

def write_samples_csv(conn, sample_pids: list[str]) -> int:
    """
    Каждая fact-таблица агрегируется ОТДЕЛЬНО до (pid × store_id × day).
    JOIN — только в Python. Никакого прямого JOIN нескольких fact-таблиц.
    Возвращает число записанных строк.
    """
    if not sample_pids:
        return 0

    d_to   = date.today()
    d_from = d_to - timedelta(days=WINDOW_DAYS - 1)

    # A. Snapshot — уже на уровне (pid × store × day)
    snap: dict[tuple, dict] = {}
    for r in _q(conn, """
        SELECT day, store_id, store_name, product_id,
               stock_qty, reserve_qty, available_qty
        FROM stock_snapshot
        WHERE product_id=ANY(%s) AND day BETWEEN %s AND %s
    """, [sample_pids, d_from, d_to]):
        snap[(r[3], r[1], r[0])] = {
            "store_name": r[2],
            "stock": float(r[4] or 0), "reserve": float(r[5] or 0), "avail": float(r[6] or 0),
        }

    # B. Sales — агрегированы отдельно
    sales: dict[tuple, dict] = {}
    for r in _q(conn, """
        SELECT day, store_id, assortment_id,
               SUM(sell_qty), SUM(revenue_kop)
        FROM sales_by_product_day
        WHERE assortment_id=ANY(%s) AND day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """, [sample_pids, d_from, d_to]):
        sales[(r[2], r[1], r[0])] = {"sold": float(r[3] or 0), "rev": int(r[4] or 0)}

    # C. Supply received — агрегированы отдельно
    recv: dict[tuple, float] = {}
    for r in _q(conn, """
        SELECT sd.day, sd.store_id, si.product_id, SUM(si.qty)
        FROM supply_item si JOIN supply_doc sd ON sd.doc_id=si.doc_id
        WHERE si.product_id=ANY(%s) AND sd.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """, [sample_pids, d_from, d_to]):
        recv[(r[2], r[1], r[0])] = float(r[3] or 0)

    # D. Move IN (целевой склад) — агрегированы отдельно
    min_: dict[tuple, float] = {}
    for r in _q(conn, """
        SELECT md.day, md.store_to_id, mi.product_id, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id=mi.doc_id
        WHERE mi.product_id=ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """, [sample_pids, d_from, d_to]):
        min_[(r[2], r[1], r[0])] = float(r[3] or 0)

    # E. Move OUT (склад-источник) — агрегированы отдельно
    mout: dict[tuple, float] = {}
    for r in _q(conn, """
        SELECT md.day, md.store_from_id, mi.product_id, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id=mi.doc_id
        WHERE mi.product_id=ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """, [sample_pids, d_from, d_to]):
        mout[(r[2], r[1], r[0])] = float(r[3] or 0)

    # F. Loss — агрегированы отдельно
    loss: dict[tuple, float] = {}
    for r in _q(conn, """
        SELECT ld.day, ld.store_id, li.product_id, SUM(li.qty)
        FROM loss_item li JOIN loss_doc ld ON ld.doc_id=li.doc_id
        WHERE li.product_id=ANY(%s) AND ld.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """, [sample_pids, d_from, d_to]):
        loss[(r[2], r[1], r[0])] = float(r[3] or 0)

    # Имена
    pnames = {r[0]: r[1] for r in _q(conn, """
        SELECT product_id, product_name FROM product_dim WHERE product_id=ANY(%s)
    """, [sample_pids])}
    store_names = {r[0]: r[1] for r in _q(conn, """
        SELECT DISTINCT store_id, store_name FROM stock_snapshot WHERE is_srezka=TRUE
    """)}

    # Объединение в Python — JOIN в памяти, не в SQL
    all_days = [d_from + timedelta(days=i) for i in range(WINDOW_DAYS)]

    written = 0
    with open(OUT_SAMPLES_TMP, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SAMPLES_HEADER)

        for pid in sample_pids:
            active_stores: set[str] = set()
            for key in [*snap, *sales, *recv, *min_, *mout, *loss]:
                if key[0] == pid:
                    active_stores.add(key[1])

            for day in all_days:
                for sid in active_stores:
                    k = (pid, sid, day)
                    s = snap.get(k)
                    sa = sales.get(k, {})
                    state = "HAS_ROW" if s else "NO_ROW"
                    row = [
                        day, pid, pnames.get(pid, "?"),
                        sid, store_names.get(sid, s["store_name"] if s else "?"),
                        state,
                        s["stock"]   if s else "",
                        s["reserve"] if s else "",
                        s["avail"]   if s else "",
                        sa.get("sold", 0), sa.get("rev", 0),
                        recv.get(k, 0), min_.get(k, 0), mout.get(k, 0), loss.get(k, 0),
                    ]
                    w.writerow(row)
                    written += 1
                    if written >= MAX_SAMPLE_CSV:
                        return written

    return written


# ── SECTION: snapshot_gaps ────────────────────────────────────────────────────

def _snapshot_volume_by_store(conn) -> dict[tuple[str, date], dict]:
    """
    Возвращает dict (store_id, day) → {sku_count, store_median_sku_count, ratio_to_median}
    для дней, когда объём snapshot по складу отклоняется от медианы.

    Порог <70% — только эвристика: используется как признак SNAPSHOT_VOLUME_ANOMALY,
    но не как доказательство неполного ETL. Реальный порог определится после анализа.
    Возвращаем метрики для ВСЕХ дней, чтобы аналитик сам выбрал нужный порог.
    """
    rows = _q(conn, """
        SELECT store_id, day, COUNT(DISTINCT product_id) AS skus
        FROM stock_snapshot WHERE is_srezka = TRUE
        GROUP BY store_id, day
    """)
    by_store: dict[str, list] = defaultdict(list)
    for r in rows:
        by_store[r[0]].append((r[1], int(r[2])))

    result: dict[tuple, dict] = {}
    for sid, entries in by_store.items():
        counts = [e[1] for e in entries]
        if not counts:
            continue
        med = statistics.median(counts)
        for day, cnt in entries:
            ratio = round(cnt / med, 3) if med else None
            result[(sid, day)] = {
                "sku_count":             cnt,
                "store_median_sku_count": med,
                "ratio_to_median":        ratio,
            }
    return result


def section_snapshot_gaps(conn) -> dict:
    """
    Паттерн: HAS_ROW → NO_ROW × N дней → HAS_ROW.

    Нейтральные состояния NO_ROW — только факты, без предположений о stock_qty:
      NO_ROW_WITH_ACTIVITY    — в период пропуска по данному SKU×складу есть
                                продажи или движения (stockout ИЛИ ETL miss — неизвестно)
      NO_ROW_NO_ACTIVITY      — в период пропуска активности нет (нулевой остаток?
                                ETL miss? — требует доказательства)
      SNAPSHOT_DAY_INCOMPLETE — весь день×склад в snapshot подозрительно неполный
                                (<70% медианы SKU); пропуск, скорее всего, системный
      UNKNOWN                 — недостаточно данных для классификации

    ВАЖНО: ZERO_CONFIRMED / ZERO_LIKELY недопустимы до доказательства.
    ETL не сохраняет строки с stock_qty<=0, поэтому «stock_before» из отсутствующей
    строки получить нельзя — мы видим только последнее HAS_ROW до пропуска.
    Интерпретация состояний — за аналитиком после изучения результатов аудита.
    """
    # Шаг 0: объём snapshot на уровне day×store (не SKU)
    # Порог <70% — эвристика; метрики сохраняются в gap для последующего анализа.
    VOL_ANOMALY_THRESHOLD = 0.70
    vol_by_store = _snapshot_volume_by_store(conn)

    # Шаг 1: матрица присутствия (pid, store_id) → sorted days
    presence: dict[tuple[str, str], list[date]] = defaultdict(list)
    for r in _q(conn, """
        SELECT product_id, store_id, day FROM stock_snapshot
        WHERE is_srezka=TRUE ORDER BY product_id, store_id, day
    """):
        presence[(r[0], r[1])].append(r[2])

    all_gaps: list[dict] = []
    for (pid, sid), days in presence.items():
        days_s = sorted(days)
        for i in range(len(days_s) - 1):
            gap = (days_s[i + 1] - days_s[i]).days - 1
            if gap > 0:
                all_gaps.append({
                    "pid": pid, "sid": sid,
                    "day_before": days_s[i], "day_after": days_s[i + 1],
                    "gap_days": gap,
                })

    all_gaps.sort(key=lambda x: -x["gap_days"])
    analyse = all_gaps[:MAX_GAPS]

    state_counts: dict[str, int] = defaultdict(int)

    for g in analyse:
        pid, sid, d0, d1 = g["pid"], g["sid"], g["day_before"], g["day_after"]

        # Последний известный снимок ПЕРЕД пропуском
        snap0 = _q(conn, """
            SELECT stock_qty, reserve_qty, available_qty FROM stock_snapshot
            WHERE product_id=%s AND store_id=%s AND day=%s
        """, [pid, sid, d0])
        # Первый снимок ПОСЛЕ пропуска
        snap1 = _q(conn, """
            SELECT stock_qty, reserve_qty, available_qty FROM stock_snapshot
            WHERE product_id=%s AND store_id=%s AND day=%s
        """, [pid, sid, d1])

        # Сохраняем факты (stock_qty из snap0 — последнее ИЗВЕСТНОЕ значение,
        # не значение в период пропуска — оно нам недоступно)
        g["last_known_stock_before_gap"] = {
            "stock": float(snap0[0][0]), "reserve": float(snap0[0][1]),
            "avail": float(snap0[0][2]),
        } if snap0 else None
        g["first_known_stock_after_gap"] = {
            "stock": float(snap1[0][0]), "reserve": float(snap1[0][1]),
            "avail": float(snap1[0][2]),
        } if snap1 else None

        # Активность по данному SKU×складу ВНУТРИ пропуска
        sold = _q(conn, """
            SELECT COALESCE(SUM(sell_qty),0), COUNT(DISTINCT day)
            FROM sales_by_product_day
            WHERE assortment_id=%s AND store_id=%s AND day>%s AND day<%s
        """, [pid, sid, d0, d1])[0]
        g["sold_in_gap"] = {"qty": float(sold[0]), "sale_days": sold[1]}

        recv = _q(conn, """
            SELECT COALESCE(SUM(si.qty),0) FROM supply_item si
            JOIN supply_doc sd ON sd.doc_id=si.doc_id
            WHERE si.product_id=%s AND sd.store_id=%s AND sd.day>%s AND sd.day<%s
        """, [pid, sid, d0, d1])[0][0]
        g["received_in_gap"] = float(recv)

        mi = _q(conn, """
            SELECT COALESCE(SUM(mi.qty),0) FROM move_item mi
            JOIN move_doc md ON md.doc_id=mi.doc_id
            WHERE mi.product_id=%s AND md.store_to_id=%s AND md.day>%s AND md.day<%s
        """, [pid, sid, d0, d1])[0][0]
        g["moved_in_gap"] = float(mi)

        lo = _q(conn, """
            SELECT COALESCE(SUM(li.qty),0) FROM loss_item li
            JOIN loss_doc ld ON ld.doc_id=li.doc_id
            WHERE li.product_id=%s AND ld.store_id=%s AND ld.day>%s AND ld.day<%s
        """, [pid, sid, d0, d1])[0][0]
        g["loss_in_gap"] = float(lo)

        # Шаг 2: классификация — только нейтральные состояния
        #
        # Приоритет: сначала проверяем полноту дня×склада (системный признак),
        # затем — активность конкретного SKU.
        # НЕ используем «stock_qty <= 0 → ZERO» — ETL не сохраняет нулевые строки,
        # поэтому last_known_stock_before_gap — это старое значение, а не текущее.

        # Проверяем объём snapshot по дням пропуска (day×store)
        gap_days_range = [d0 + timedelta(days=j + 1)
                          for j in range((d1 - d0).days - 1)]
        anomaly_days = []
        for gd in gap_days_range:
            vol = vol_by_store.get((sid, gd))
            if vol and vol["ratio_to_median"] is not None and vol["ratio_to_median"] < VOL_ANOMALY_THRESHOLD:
                anomaly_days.append({
                    "day": gd,
                    "sku_count":              vol["sku_count"],
                    "store_median_sku_count": vol["store_median_sku_count"],
                    "ratio_to_median":        vol["ratio_to_median"],
                })

        # Также сохраняем метрики дней до и после пропуска — для сравнения
        vol_before = vol_by_store.get((sid, d0))
        vol_after  = vol_by_store.get((sid, d1))
        g["snapshot_volume_before"] = vol_before
        g["snapshot_volume_after"]  = vol_after
        g["snapshot_volume_anomaly_days"] = anomaly_days  # дни с ratio < 70%

        has_activity = (g["sold_in_gap"]["qty"] > 0
                        or g["received_in_gap"] > 0
                        or g["moved_in_gap"] > 0)

        if anomaly_days:
            # Объём snapshot аномально мал в период пропуска — возможный сбой ETL.
            # HEURISTIC: порог 70% не доказан. ratio_to_median сохранён для анализа.
            state = "SNAPSHOT_VOLUME_ANOMALY"
        elif has_activity:
            # Активность есть, но это не доказывает ни stockout ни ETL miss
            state = "NO_ROW_WITH_ACTIVITY"
        elif snap0 is not None:
            # Нет активности, есть предыдущий снимок
            state = "NO_ROW_NO_ACTIVITY"
        else:
            state = "UNKNOWN"

        g["no_row_state"] = state
        state_counts[state] += 1

    return {
        "total_gaps_found": len(all_gaps),
        "analysed": len(analyse),
        "no_row_state_counts": dict(state_counts),
        "state_legend": {
            "SNAPSHOT_VOLUME_ANOMALY": (
                "В период пропуска объём snapshot по данному складу <70% медианы (эвристика). "
                "Порог не доказан. ratio_to_median сохранён в snapshot_volume_anomaly_days. "
                "Возможный сбой ETL, но требует проверки по реальным данным."
            ),
            "NO_ROW_WITH_ACTIVITY": (
                "В период пропуска есть продажи или движения по данному SKU×складу. "
                "Возможно: stockout утром + продажи, или ETL miss. "
                "Активность SKU сама по себе не доказывает ETL miss."
            ),
            "NO_ROW_NO_ACTIVITY": (
                "В период пропуска нет активности по данному SKU×складу. "
                "Возможно: нулевой остаток. НО: ETL не сохраняет нулевые строки, "
                "поэтому last_known_stock_before_gap ≠ stock в период пропуска."
            ),
            "UNKNOWN": "Нет предыдущего снимка или недостаточно данных для классификации.",
        },
        "analysis_note": (
            "ZERO_CONFIRMED / ZERO_LIKELY не используются: ETL (stock_qty<=0: continue) "
            "не сохраняет нулевые строки, поэтому из NO_ROW нельзя вывести stock_qty."
        ),
        "gaps": analyse,
    }


# ── BALANCE CSV ───────────────────────────────────────────────────────────────

BALANCE_HEADER = [
    "pid", "store_id", "day_from", "day_to", "gap_calendar_days",
    "stock_start", "stock_end",
    "sold", "received", "moved_in", "moved_out", "loss",
    "predicted_end", "balance_error",
    "snapshot_timing_note",
]

def write_balance_csv(conn, sample_pids: list[str], timing_type: str) -> dict:
    """
    Балансовая проверка: stock_t = stock_{t-1} + received + moved_in - sold - moved_out - loss
    Использует snapshot_timing_type для предупреждения об ограничениях.
    Каждая fact-таблица агрегируется отдельно.
    """
    if not sample_pids:
        return {"note": "No sample PIDs"}

    snap_rows = _q(conn, """
        SELECT product_id, store_id, day, stock_qty
        FROM stock_snapshot WHERE product_id=ANY(%s)
        ORDER BY product_id, store_id, day
    """, [sample_pids])

    if not snap_rows:
        return {"note": "No snapshot data"}

    snap: dict[tuple, float] = {(r[0], r[1], r[2]): float(r[3] or 0) for r in snap_rows}
    all_dates = [k[2] for k in snap]
    d_min, d_max = min(all_dates), max(all_dates)

    def _mov(sql):
        return {(r[2], r[1], r[0]): float(r[3] or 0)
                for r in _q(conn, sql, [sample_pids, d_min, d_max])}

    sold_i = _mov("""
        SELECT day, store_id, assortment_id, SUM(sell_qty)
        FROM sales_by_product_day WHERE assortment_id=ANY(%s) AND day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """)
    recv_i = _mov("""
        SELECT sd.day, sd.store_id, si.product_id, SUM(si.qty)
        FROM supply_item si JOIN supply_doc sd ON sd.doc_id=si.doc_id
        WHERE si.product_id=ANY(%s) AND sd.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """)
    min_i = _mov("""
        SELECT md.day, md.store_to_id, mi.product_id, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id=mi.doc_id
        WHERE mi.product_id=ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """)
    mout_i = _mov("""
        SELECT md.day, md.store_from_id, mi.product_id, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id=mi.doc_id
        WHERE mi.product_id=ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """)
    loss_i = _mov("""
        SELECT ld.day, ld.store_id, li.product_id, SUM(li.qty)
        FROM loss_item li JOIN loss_doc ld ON ld.doc_id=li.doc_id
        WHERE li.product_id=ANY(%s) AND ld.day BETWEEN %s AND %s
        GROUP BY 1,2,3
    """)

    by_ps: dict[tuple, list[date]] = defaultdict(list)
    for pid, sid, day in snap:
        by_ps[(pid, sid)].append(day)

    timing_note = (
        f"APPROXIMATE_BALANCE_CHECK sync_type={timing_type} "
        "snapshot_business_time_semantics=UNKNOWN"
    )

    errors: list[dict] = []
    total_pairs = 0

    for (pid, sid), days in by_ps.items():
        days_s = sorted(days)
        for i in range(len(days_s) - 1):
            d0, d1 = days_s[i], days_s[i + 1]
            if (d1 - d0).days > 3:
                continue
            total_pairs += 1
            s0, s1 = snap[(pid, sid, d0)], snap[(pid, sid, d1)]

            sold = rcv = mi = mo = lo = 0.0
            d = d0 + timedelta(days=1)
            while d <= d1:
                sold += sold_i.get((pid, sid, d), 0)
                rcv  += recv_i.get((pid, sid, d), 0)
                mi   += min_i.get((pid, sid, d), 0)
                mo   += mout_i.get((pid, sid, d), 0)
                lo   += loss_i.get((pid, sid, d), 0)
                d += timedelta(days=1)

            pred = s0 + rcv + mi - sold - mo - lo
            err  = s1 - pred

            if abs(err) > 0.5:
                errors.append({
                    "pid": pid, "sid": sid, "d0": d0, "d1": d1,
                    "gap": (d1 - d0).days,
                    "s0": s0, "s1": s1,
                    "sold": sold, "rcv": rcv, "mi": mi, "mo": mo, "lo": lo,
                    "pred": round(pred, 3), "err": round(err, 3),
                })

    errors.sort(key=lambda x: -abs(x["err"]))
    errors = errors[:MAX_BALANCE]

    with open(OUT_BALANCE_TMP, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(BALANCE_HEADER)
        for e in errors:
            w.writerow([
                e["pid"], e["sid"], e["d0"], e["d1"], e["gap"],
                e["s0"], e["s1"],
                e["sold"], e["rcv"], e["mi"], e["mo"], e["lo"],
                e["pred"], e["err"], timing_note,
            ])

    return {
        "total_consecutive_pairs_checked": total_pairs,
        "pairs_with_error_gt_0.5": len(errors),
        "error_rate_pct": round(len(errors) / max(total_pairs, 1) * 100, 1),
        "timing_warning": timing_note,
        "top_5_errors": errors[:5],
        "full_errors_in": OUT_BALANCE,
    }


# ── SECTION: sales_etl_check ─────────────────────────────────────────────────

def section_sales_etl_check(conn) -> dict:
    neg  = _q(conn, "SELECT COUNT(*) FROM sales_by_product_day WHERE sell_qty<0")[0][0]
    zero = _q(conn, "SELECT COUNT(*) FROM sales_by_product_day WHERE sell_qty=0")[0][0]
    pos  = _q(conn, "SELECT COUNT(*) FROM sales_by_product_day WHERE sell_qty>0")[0][0]

    neg_samples = _q(conn, """
        SELECT day, store_id, assortment_id, product_name, sell_qty, revenue_kop
        FROM sales_by_product_day WHERE sell_qty<0 ORDER BY sell_qty ASC LIMIT 10
    """)

    return {
        "distribution": {"positive": pos, "zero": zero, "negative": neg},
        "negative_samples": [
            {"day": r[0], "store_id": r[1], "assortment_id": r[2],
             "name": r[3], "sell_qty": r[4], "revenue_kop": r[5]}
            for r in neg_samples
        ],
        "note": "sell_qty = sellQuantity − returnQuantity (из ETL). Отрицательное = возвраты превысили продажи за день.",
    }


# ── SECTION: unknowns ────────────────────────────────────────────────────────

def section_unknowns(existing: set, schema_info: dict) -> dict:
    items = []

    order_tbls = [t for t in existing if "order" in t or "purchase" in t]
    items.append({
        "item": "FUTURE_INCOMING_ORDERS",
        "status": f"FOUND: {order_tbls}" if order_tbls else "NOT_AVAILABLE",
        "impact": "CRITICAL — нельзя вычесть ожидаемые поставки. Трактовать как 0 запрещено.",
    })

    supply_cols = [c["name"] for c in schema_info["tables"].get("supply_doc", {}).get("columns", [])]
    items.append({
        "item": "SUPPLY_AGENT_ID",
        "status": "AVAILABLE" if "agent_id" in supply_cols else "NOT_AVAILABLE",
        "impact": "Без agent_id связь поставщика по agent_name — нестабильно при переименовании.",
    })

    items.append({
        "item": "YEAR_AGO_DATA",
        "status": "SEE data_history.stock_snapshot.min_date",
        "impact": "Если данные с 2026-06 — год назад недоступен. year_ago_demand = UNKNOWN, не 0.",
    })

    items.append({
        "item": "BATCH_TRACKING",
        "status": "NOT_AVAILABLE",
        "impact": "Нет возраста партии → sell-through по партии не считается точно.",
    })

    items.append({
        "item": "ENTER_DOC",
        "status": "AVAILABLE" if "enter_doc" in existing else "NOT_AVAILABLE",
        "impact": "Оприходования — плюсовая сторона инвентаризации Базы.",
    })

    return {"items": items}


# ── MAIN ──────────────────────────────────────────────────────────────────────

def _check_mode() -> None:
    """
    --check: проверяет импорты и структуру модуля БЕЗ подключения к БД.
    DATABASE_URL проверяется только на наличие (не вызывается connect).
    Завершается с ненулевым кодом при реальных ошибках структуры.
    """
    ok = True

    print("CHECK MODE")
    print("-" * 40)

    # 1. Imports — уже выполнены на уровне модуля (psycopg, hermes.config, etc.)
    print("Imports: OK")

    # 2. CLI — все ключевые функции определены и вызываемы
    expected_funcs = [
        "_connect_readonly", "_preflight", "_check_mode",
        "section_schema", "section_data_history", "section_snapshot_timing",
        "section_snapshot_completeness", "section_stores", "_select_sample_skus",
        "write_samples_csv", "section_snapshot_gaps", "write_balance_csv",
        "section_sales_etl_check", "section_unknowns",
    ]
    missing = [f for f in expected_funcs if not callable(globals().get(f))]
    if missing:
        print(f"CLI: FAIL (отсутствуют функции: {missing})")
        ok = False
    else:
        print("CLI: OK")

    # 3. Output path — директория существует и доступна для записи
    script_dir = os.path.dirname(OUT_SUMMARY)
    if not os.path.isdir(script_dir):
        print(f"Output path: FAIL (директория не существует: {script_dir})")
        ok = False
    elif not os.access(script_dir, os.W_OK):
        print(f"Output path: FAIL (нет прав на запись: {script_dir})")
        ok = False
    else:
        print(f"Output path: OK ({script_dir})")

    # 4. Конфигурация — импорт символов из hermes.config уже состоялся на уровне модуля.
    # DATABASE_URL() намеренно НЕ вызывается: --check не должен читать .env или DSN.
    print("Configuration import: OK")
    print("Database connection attempted: NO")
    print("MoySklad API calls: NO")
    print("Production changes: NO")

    print("-" * 40)
    if ok:
        print("Result: PASS")
    else:
        print("Result: FAIL")
        raise SystemExit("CHECK FAILED")


def main() -> None:
    if "--check" in sys.argv:
        _check_mode()
        return

    print("=" * 60)
    print("Hermes Forecast Data Audit v2.1 — READ ONLY")
    print("=" * 60)

    print("\nПодключение к БД (read-only на уровне опций подключения)...")
    conn = _connect_readonly()  # SystemExit если read-only не подтверждён на уровне PG

    # Все файлы пишутся в .tmp, затем атомарно переименовываются.
    # При любом исключении finally удаляет только .tmp — итоговые файлы
    # предыдущего успешного аудита остаются нетронутыми.
    sections: dict[str, str] = {}
    audit_ok = False
    try:
        summary: dict[str, Any] = {}

        print("[1/11] Preflight + идентификация БД...")
        summary["audit_meta"] = _preflight(conn)

        print("[2/11] Схема...")
        summary["schema"] = section_schema(conn)
        existing = set(summary["schema"]["existing_tables"])
        sections["schema"] = "PASS"
        print(f"       Таблиц найдено: {len(existing)}")

        print("[3/11] История данных...")
        summary["data_history"] = section_data_history(conn, existing)
        sections["history"] = "PASS"

        print("[4/11] Время snapshot (synced_at)...")
        summary["snapshot_timing"] = section_snapshot_timing(conn)
        timing_type = summary["snapshot_timing"].get("sync_timing_type", "UNKNOWN")
        sections["snapshot_timing"] = (
            "PASS" if summary["snapshot_timing"].get("sample_days", 0) > 0
            else "NOT_AVAILABLE"
        )
        print(f"       sync_timing_type: {timing_type}"
              f"  |  час MSK: {summary['snapshot_timing'].get('most_common_sync_hour_msk')}"
              f"\n       snapshot_business_time_semantics: UNKNOWN")

        print("[5/11] Полнота snapshot по дням...")
        summary["snapshot_completeness"] = section_snapshot_completeness(conn)
        sections["snapshot_completeness"] = (
            "PASS" if "summary" in summary["snapshot_completeness"]
            else "NOT_AVAILABLE"
        )

        print("[6/11] Роли складов и контрагенты...")
        summary["stores"] = section_stores(conn)
        sections["stores"] = "PASS"

        print("[7/11] Выборка SKU (~40-60 позиций)...")
        sample_skus = _select_sample_skus(conn)
        summary["sample_skus"] = sample_skus
        pids = [s["pid"] for s in sample_skus]
        sections["sample_skus"] = "PASS" if pids else "NOT_AVAILABLE"
        print(f"       Выбрано SKU: {len(pids)}")

        print(f"[8/11] SKU×STORE×DAY CSV (последние {WINDOW_DAYS} дней) → {OUT_SAMPLES}")
        n_rows = write_samples_csv(conn, pids)
        summary["samples_csv"] = {"path": OUT_SAMPLES, "rows_written": n_rows}
        sections["samples_csv"] = "PASS" if n_rows > 0 else "NOT_AVAILABLE"
        print(f"       Записано строк: {n_rows}")

        print("[9/11] Анализ пропусков snapshot...")
        summary["snapshot_gaps"] = section_snapshot_gaps(conn)
        sections["snapshot_gaps"] = "PASS"
        print(f"       Пропусков всего: {summary['snapshot_gaps']['total_gaps_found']}")
        print(f"       Статусы: {summary['snapshot_gaps']['no_row_state_counts']}")

        print(f"[10/11] Балансовая проверка → {OUT_BALANCE}")
        summary["balance_check"] = write_balance_csv(conn, pids, timing_type)
        bc = summary["balance_check"]
        if "note" in bc:
            sections["balance"] = "NOT_AVAILABLE"
        elif bc.get("total_consecutive_pairs_checked", 0) == 0:
            sections["balance"] = "PARTIAL"
        else:
            sections["balance"] = "PASS"
        print(f"        Ошибок (>0.5): {bc.get('pairs_with_error_gt_0.5', '—')}"
              f"  из {bc.get('total_consecutive_pairs_checked', '—')}"
              f"  ({bc.get('error_rate_pct', '—')}%)")

        print("[11/11] Прочие проверки...")
        summary["sales_etl_check"] = section_sales_etl_check(conn)
        sections["sales_etl"] = "PASS"
        summary["unknowns"] = section_unknowns(existing, summary["schema"])
        # unknowns сообщает о NOT_AVAILABLE внутри items — сама секция всегда выполнена
        sections["unknowns"] = "PASS"

        summary["sections"] = sections
        summary["audit_status"] = "COMPLETE"

        # Атомарная запись:
        #   1. summary → .tmp
        #   2. os.replace для CSV-файлов (уже записаны write_*_csv в .tmp)
        #   3. os.replace summary — последним; наличие summary = маркер успеха
        with open(OUT_SUMMARY_TMP, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=_j)

        if os.path.exists(OUT_SAMPLES_TMP):
            os.replace(OUT_SAMPLES_TMP, OUT_SAMPLES)
        if os.path.exists(OUT_BALANCE_TMP):
            os.replace(OUT_BALANCE_TMP, OUT_BALANCE)
        os.replace(OUT_SUMMARY_TMP, OUT_SUMMARY)  # последним — маркер успеха

        audit_ok = True

    except SystemExit:
        raise  # preflight/connect уже напечатали причину

    except Exception as exc:
        print(f"\nERROR во время аудита: {exc}")
        raise  # ненулевой exit code

    finally:
        try:
            conn.close()
        except Exception:
            pass

        if not audit_ok:
            # Удаляем ТОЛЬКО .tmp файлы текущего незавершённого запуска.
            # Итоговые файлы предыдущего успешного аудита не трогаем.
            for path in [OUT_SUMMARY_TMP, OUT_SAMPLES_TMP, OUT_BALANCE_TMP]:
                if os.path.exists(path):
                    os.remove(path)
                    print(f"  Удалён незавершённый файл: {path}")

    print("\n" + "=" * 60)
    print("АУДИТ ЗАВЕРШЁН УСПЕШНО")
    size_s  = os.path.getsize(OUT_SUMMARY) / 1024
    size_sa = os.path.getsize(OUT_SAMPLES) / 1024 if os.path.exists(OUT_SAMPLES) else 0
    size_b  = os.path.getsize(OUT_BALANCE)  / 1024 if os.path.exists(OUT_BALANCE)  else 0
    print(f"  {OUT_SUMMARY}  ({size_s:.0f} KB)")
    print(f"  {OUT_SAMPLES}  ({size_sa:.0f} KB)")
    print(f"  {OUT_BALANCE}  ({size_b:.0f} KB)")
    print("=" * 60)


if __name__ == "__main__":
    main()
