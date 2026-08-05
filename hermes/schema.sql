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

-- Списания: заголовки документов списания МойСклад
CREATE TABLE IF NOT EXISTS loss_doc (
    doc_id       text        PRIMARY KEY,
    moment       timestamptz NOT NULL,
    day          date        NOT NULL,
    store_id     text        NOT NULL,
    store_name   text        NOT NULL,
    description  text,
    synced_at    timestamptz NOT NULL DEFAULT now()
);

-- Списания: позиции документов (что именно списали)
CREATE TABLE IF NOT EXISTS loss_item (
    doc_id       text        NOT NULL REFERENCES loss_doc(doc_id) ON DELETE CASCADE,
    position_id  text        NOT NULL,
    product_name text        NOT NULL,
    folder_path  text,
    qty          numeric(14,3) NOT NULL DEFAULT 0,
    cost_kop     bigint      NOT NULL DEFAULT 0,  -- себест. единицы в копейках
    total_kop    bigint      NOT NULL DEFAULT 0,  -- qty × cost
    synced_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, position_id)
);

CREATE INDEX IF NOT EXISTS ix_loss_doc_day      ON loss_doc (day);
CREATE INDEX IF NOT EXISTS ix_loss_doc_store    ON loss_doc (store_id, day);
CREATE INDEX IF NOT EXISTS ix_loss_item_product ON loss_item (product_name);

-- Поставки: заголовки входящих поставок (supply)
CREATE TABLE IF NOT EXISTS supply_doc (
    doc_id       text        PRIMARY KEY,
    moment       timestamptz NOT NULL,
    day          date        NOT NULL,
    store_id     text        NOT NULL,
    store_name   text        NOT NULL,
    agent_name   text,                            -- поставщик
    description  text,
    total_kop    bigint      NOT NULL DEFAULT 0,  -- сумма поставки
    synced_at    timestamptz NOT NULL DEFAULT now()
);

-- Поставки: позиции
CREATE TABLE IF NOT EXISTS supply_item (
    doc_id       text        NOT NULL REFERENCES supply_doc(doc_id) ON DELETE CASCADE,
    position_id  text        NOT NULL,
    product_name text        NOT NULL,
    qty          numeric(14,3) NOT NULL DEFAULT 0,
    price_kop    bigint      NOT NULL DEFAULT 0,  -- закупочная цена единицы
    total_kop    bigint      NOT NULL DEFAULT 0,
    synced_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, position_id)
);

CREATE INDEX IF NOT EXISTS ix_supply_doc_day    ON supply_doc (day);
CREATE INDEX IF NOT EXISTS ix_supply_doc_store  ON supply_doc (store_id, day);
CREATE INDEX IF NOT EXISTS ix_supply_doc_agent  ON supply_doc (agent_name);

-- Движение денег: входящие и исходящие платежи (кассовые + банковские)
CREATE TABLE IF NOT EXISTS cashflow_event (
    event_id     text        PRIMARY KEY,
    moment       timestamptz NOT NULL,
    day          date        NOT NULL,
    direction    text        NOT NULL,            -- 'in' | 'out'
    doc_type     text        NOT NULL,            -- cashin | cashout | paymentin | paymentout
    agent_name   text,
    description  text,
    amount_kop   bigint      NOT NULL DEFAULT 0,
    synced_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cashflow_day       ON cashflow_event (day);
CREATE INDEX IF NOT EXISTS ix_cashflow_direction ON cashflow_event (direction, day);

-- Статья расходов (expenseItem из МойСклад, для cashout/paymentout)
ALTER TABLE cashflow_event ADD COLUMN IF NOT EXISTS expense_item_name text;
-- Проект/подразделение (project из МойСклад, для привязки к складу)
ALTER TABLE cashflow_event ADD COLUMN IF NOT EXISTS project_name text;
-- Проект у документа списания (указывает подразделение/склад/цель)
ALTER TABLE loss_doc ADD COLUMN IF NOT EXISTS project_name text;

-- Документы отгрузки (demand) для клиентской аналитики
CREATE TABLE IF NOT EXISTS sales_doc (
    doc_id       text        PRIMARY KEY,
    moment       timestamptz NOT NULL,
    day          date        NOT NULL,
    store_id     text        NOT NULL,
    channel      text        NOT NULL DEFAULT '',  -- розница | опт | ресторан
    agent_id     text        NOT NULL DEFAULT '',
    agent_name   text        NOT NULL DEFAULT '',
    sum_kop      bigint      NOT NULL DEFAULT 0,
    synced_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sales_doc ADD COLUMN IF NOT EXISTS store_name text;
ALTER TABLE sales_doc ADD COLUMN IF NOT EXISTS positions  integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_sales_doc_day   ON sales_doc (day);
CREATE INDEX IF NOT EXISTS ix_sales_doc_agent ON sales_doc (agent_id, day);
