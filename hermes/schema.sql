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

-- Справочник товаров: id → folder_path/is_srezka. Обновляется при каждом etl_stock.
-- Нужен для связи sales_by_product_day (assortment_id) с категорией товара.
CREATE TABLE IF NOT EXISTS product_dim (
    product_id   text        PRIMARY KEY,
    product_name text        NOT NULL,
    folder_path  text,
    is_srezka    boolean     NOT NULL DEFAULT false,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Праздничный календарь: конкретные даты (не правило «каждый год»).
-- lead_days — за сколько дней до даты начинается ажиотажный спрос.
-- fallback_multiplier — используется, если истории прошлого года нет.
-- Дима пополняет список раз в год под каждый следующий год.
CREATE TABLE IF NOT EXISTS holiday (
    holiday_date        date           PRIMARY KEY,
    name                text           NOT NULL,
    lead_days           integer        NOT NULL DEFAULT 3,
    fallback_multiplier numeric(4, 2)  NOT NULL DEFAULT 2.0
);

-- Стартовый набор праздников 2026–2027 (корректировать по факту):
INSERT INTO holiday (holiday_date, name, lead_days, fallback_multiplier) VALUES
    ('2026-02-14', '14 февраля',       3, 1.5),
    ('2026-03-08', '8 марта',          5, 4.0),
    ('2026-05-25', 'Последний звонок', 2, 2.0),
    ('2026-09-01', '1 сентября',       2, 2.0),
    ('2026-10-04', 'День учителя',     2, 1.5),
    ('2026-11-29', 'День матери',      2, 2.0),
    ('2026-12-31', 'Новый год',        3, 2.0),
    ('2027-02-14', '14 февраля',       3, 1.5),
    ('2027-03-08', '8 марта',          5, 4.0),
    ('2027-05-25', 'Последний звонок', 2, 2.0),
    ('2027-09-01', '1 сентября',       2, 2.0),
    ('2027-10-03', 'День учителя',     2, 1.5),
    ('2027-11-28', 'День матери',      2, 2.0),
    ('2027-12-31', 'Новый год',        3, 2.0)
ON CONFLICT (holiday_date) DO NOTHING;

-- Перемещения между складами (/entity/move).
-- Канал «ресторан» (СОБРАНИЕ) работает через перемещения, поэтому без них
-- цифры по СОБРАНИЮ недостоверны, а управленческая прибыль невозможна.
CREATE TABLE IF NOT EXISTS move_doc (
    doc_id           text        PRIMARY KEY,
    moment           timestamptz NOT NULL,
    day              date        NOT NULL,
    store_from_id    text        NOT NULL DEFAULT '',
    store_from_name  text        NOT NULL DEFAULT '',
    store_to_id      text        NOT NULL DEFAULT '',
    store_to_name    text        NOT NULL DEFAULT '',
    description      text,
    total_kop        bigint      NOT NULL DEFAULT 0,   -- сумма перемещения (по себест.)
    synced_at        timestamptz NOT NULL DEFAULT now()
);

-- Перемещения: позиции
CREATE TABLE IF NOT EXISTS move_item (
    doc_id       text        NOT NULL REFERENCES move_doc(doc_id) ON DELETE CASCADE,
    position_id  text        NOT NULL,
    product_name text        NOT NULL,
    qty          numeric(14,3) NOT NULL DEFAULT 0,
    cost_kop     bigint      NOT NULL DEFAULT 0,   -- себест. единицы в копейках
    total_kop    bigint      NOT NULL DEFAULT 0,   -- qty × cost
    synced_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, position_id)
);

CREATE INDEX IF NOT EXISTS ix_move_doc_day       ON move_doc (day);
CREATE INDEX IF NOT EXISTS ix_move_doc_from       ON move_doc (store_from_id, day);
CREATE INDEX IF NOT EXISTS ix_move_doc_to         ON move_doc (store_to_id, day);
CREATE INDEX IF NOT EXISTS ix_move_item_product   ON move_item (product_name);
