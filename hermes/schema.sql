-- Схема хранилища Hermes. Деньги храним в КОПЕЙКАХ (integer), чтобы не терять
-- копейки на округлении. В рубли переводим только при выводе отчёта.

-- Продажи по складу за день: агрегат, из которого считается вся выручка/прибыль.
CREATE TABLE IF NOT EXISTS sales_by_store_day (
    day             date        NOT NULL,
    store_id        text        NOT NULL,
    store_name      text        NOT NULL,
    channel         text        NOT NULL,          -- розница | опт | ресторан
    revenue_kop     bigint      NOT NULL DEFAULT 0, -- выручка (sellSum - returnSum)
    cost_kop        bigint      NOT NULL DEFAULT 0, -- себестоимость (sellCostSum - returnCostSum)
    checks          integer     NOT NULL DEFAULT 0, -- число чеков (документов отгрузки)
    positions_total integer     NOT NULL DEFAULT 0, -- всего товарных позиций
    positions_nocost integer    NOT NULL DEFAULT 0, -- из них без себестоимости (для оценки доверия)
    synced_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, store_id)
);

-- Продажи по товару за день и склад: для топ-20 и разбивок по категориям.
CREATE TABLE IF NOT EXISTS sales_by_product_day (
    day           date    NOT NULL,
    store_id      text    NOT NULL,
    assortment_id text    NOT NULL,
    product_name  text    NOT NULL,
    sell_qty      double precision NOT NULL DEFAULT 0,
    revenue_kop   bigint  NOT NULL DEFAULT 0,
    cost_kop      bigint  NOT NULL DEFAULT 0,
    profit_kop    bigint  NOT NULL DEFAULT 0,
    PRIMARY KEY (day, store_id, assortment_id)
);

-- Журнал выгрузок: что запрашивали, сколько получили, сколько заняло, ошибки.
CREATE TABLE IF NOT EXISTS sync_log (
    id          bigserial   PRIMARY KEY,
    task        text        NOT NULL,
    period_from date,
    period_to   date,
    rows_loaded integer,
    duration_ms integer,
    ok          boolean     NOT NULL,
    error       text,
    started_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_sales_store_day_channel ON sales_by_store_day (channel, day);
CREATE INDEX IF NOT EXISTS ix_sales_product_day ON sales_by_product_day (day, store_id);

-- Снимок остатков по товару × склад за день.
-- Себестоимость: price из report/stock/all (средневзвешенная закупочная цена).
CREATE TABLE IF NOT EXISTS stock_snapshot (
    day              date             NOT NULL,
    store_id         text             NOT NULL,
    store_name       text             NOT NULL,
    product_id       text             NOT NULL,
    product_name     text             NOT NULL,
    folder_id        text,
    folder_path      text,                          -- полный путь группы (для поиска СРЕЗКИ)
    is_srezka        boolean          NOT NULL DEFAULT false,
    stock_qty        numeric(14,3)    NOT NULL DEFAULT 0,
    reserve_qty      numeric(14,3)    NOT NULL DEFAULT 0,
    available_qty    numeric(14,3)    NOT NULL DEFAULT 0,
    cost_price_kop   bigint           NOT NULL DEFAULT 0,  -- себест. единицы в копейках
    synced_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (day, store_id, product_id)
);

CREATE INDEX IF NOT EXISTS ix_stock_snapshot_day        ON stock_snapshot (day);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_srezka_day ON stock_snapshot (is_srezka, day);
