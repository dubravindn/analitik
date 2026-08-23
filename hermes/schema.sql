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

-- Товар в позиции списания (id из МойСклад) — join по имени ненадёжен (дубли).
-- id брать чистым (.split('?')[0]) — урок бага ?expand=supplier.
ALTER TABLE loss_item ADD COLUMN IF NOT EXISTS product_id text;
CREATE INDEX IF NOT EXISTS ix_loss_item_pid ON loss_item (product_id);

-- Цены из карточки товара МойСклад (снимок раз в сутки → история цен).
-- Блок G: закупочная цена берётся отсюда (buyPrice), а не из приёмок.
CREATE TABLE IF NOT EXISTS product_price (
    day            date        NOT NULL,
    product_id     text        NOT NULL,
    product_name   text        NOT NULL,
    buy_price_kop  bigint      NOT NULL DEFAULT 0,   -- закупочная (buyPrice)
    min_price_kop  bigint      NOT NULL DEFAULT 0,   -- минимальная (minPrice)
    sale_prices    jsonb,                            -- {"Наличка":9900,"Розница":19900,...}
    synced_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, product_id)
);
CREATE INDEX IF NOT EXISTS ix_product_price_pid ON product_price (product_id, day);

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

-- Товар в позиции поставки (id из МойСклад) — для связи с продажами/остатками
-- по закупочной цене приёмки (D0.1). По названию связывать нельзя (дубли).
ALTER TABLE supply_item ADD COLUMN IF NOT EXISTS product_id text;
CREATE INDEX IF NOT EXISTS ix_supply_item_product ON supply_item (product_id);

-- Закупочная цена из КАРТОЧКИ товара (блок G): buy_price_kop из снимков
-- product_price. Для операции дня D берётся строка с максимальным priced_from<=D
-- (см. calc.purchase_price_at). Плюс «фолбэк-строка» от 2000-01-01 с самым ранним
-- снимком каждого товара — чтобы продажи ДО начала снятия снимков тоже покрывались
-- (приблизительно, самой ранней известной ценой). Так покрытие ~100% без правок
-- логики asof в отчётах.
CREATE OR REPLACE VIEW purchase_price_asof AS
    SELECT product_id, day AS priced_from, buy_price_kop AS price_kop
    FROM product_price
    WHERE buy_price_kop > 0
UNION ALL
    SELECT product_id, DATE '2000-01-01' AS priced_from, price_kop
    FROM (
        SELECT DISTINCT ON (product_id) product_id, buy_price_kop AS price_kop
        FROM product_price
        WHERE buy_price_kop > 0
        ORDER BY product_id, day ASC
    ) earliest;

-- Оптовая цена продажи «Наличка» из карточки (блок I) — asof так же, как закупочная.
CREATE OR REPLACE VIEW nal_price_asof AS
    SELECT product_id, day AS priced_from, (sale_prices->>'Наличка')::bigint AS price_kop
    FROM product_price
    WHERE (sale_prices->>'Наличка') ~ '^[0-9]+$' AND (sale_prices->>'Наличка')::bigint > 0
UNION ALL
    SELECT product_id, DATE '2000-01-01' AS priced_from, price_kop
    FROM (
        SELECT DISTINCT ON (product_id) product_id, (sale_prices->>'Наличка')::bigint AS price_kop
        FROM product_price
        WHERE (sale_prices->>'Наличка') ~ '^[0-9]+$' AND (sale_prices->>'Наличка')::bigint > 0
        ORDER BY product_id, day ASC
    ) earliest;

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
-- Тип документа: 'demand' (отгрузка) | 'salesreturn' (возврат, sum_kop < 0).
-- Для нетто-сумм по клиенту и корректного детектора оттока (интервалы — только по demand).
ALTER TABLE sales_doc ADD COLUMN IF NOT EXISTS doc_type text NOT NULL DEFAULT 'demand';

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

-- Товар в позиции перемещения (id из МойСклад) — для пересчёта по закупочным
-- ценам из приёмок (E2). id брать чистым (без ?expand=…), см. баг остатков.
ALTER TABLE move_item ADD COLUMN IF NOT EXISTS product_id text;
CREATE INDEX IF NOT EXISTS ix_move_item_pid ON move_item (product_id);

-- ── Оприходования (enter) — «+»-сторона инвентаризации (H2.3/J2) ──────────────
-- Зеркало loss: заголовки документов оприходования МойСклад (/entity/enter).
-- Нужны, чтобы видеть инвентаризацию Базы в обе стороны: списано vs оприходовано.
CREATE TABLE IF NOT EXISTS enter_doc (
    doc_id       text        PRIMARY KEY,
    moment       timestamptz NOT NULL,
    day          date        NOT NULL,
    store_id     text        NOT NULL,
    store_name   text        NOT NULL,
    description  text,
    project_name text,
    synced_at    timestamptz NOT NULL DEFAULT now()
);

-- Оприходования: позиции (что именно оприходовали)
CREATE TABLE IF NOT EXISTS enter_item (
    doc_id       text        NOT NULL REFERENCES enter_doc(doc_id) ON DELETE CASCADE,
    position_id  text        NOT NULL,
    product_id   text,
    product_name text        NOT NULL,
    folder_path  text,
    qty          numeric(14,3) NOT NULL DEFAULT 0,
    cost_kop     bigint      NOT NULL DEFAULT 0,  -- цена оприходования единицы (коп.)
    total_kop    bigint      NOT NULL DEFAULT 0,  -- qty × cost
    synced_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, position_id)
);

CREATE INDEX IF NOT EXISTS ix_enter_doc_day     ON enter_doc (day);
CREATE INDEX IF NOT EXISTS ix_enter_doc_store   ON enter_doc (store_id, day);
CREATE INDEX IF NOT EXISTS ix_enter_item_pid    ON enter_item (product_id);

-- ── Независимый AI-анализ отчётов ──────────────────────────────────────────
-- В payload хранятся только структурированные факты отчёта. Токены МойСклад,
-- Telegram и данные авторизации Codex сюда никогда не записываются.
CREATE TABLE IF NOT EXISTS ai_analysis_run (
    id              text        PRIMARY KEY,
    report_type     text        NOT NULL,
    report_id       text        NOT NULL,
    chat_id         text        NOT NULL,
    payload_hash    text        NOT NULL,
    prompt_version  text        NOT NULL,
    model           text        NOT NULL,
    mode            text        NOT NULL CHECK (mode IN ('shadow', 'live')),
    status          text        NOT NULL,
    payload_json    jsonb       NOT NULL,
    raw_response    text,
    validated_json  jsonb,
    error           text,
    duration_ms     integer,
    attempts        integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz
);

CREATE INDEX IF NOT EXISTS ix_ai_analysis_report
    ON ai_analysis_run (report_type, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_ai_analysis_status
    ON ai_analysis_run (status, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_feedback (
    run_id      text        NOT NULL REFERENCES ai_analysis_run(id) ON DELETE CASCADE,
    chat_id     text        NOT NULL,
    value       text        NOT NULL CHECK (value IN ('up', 'down')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, chat_id)
);

-- Связь результата AI с сообщением Telegram: позволяет владельцу ответить
-- непосредственно на конкретный анализ.
CREATE TABLE IF NOT EXISTS ai_delivery (
    run_id       text        NOT NULL REFERENCES ai_analysis_run(id) ON DELETE CASCADE,
    chat_id      text        NOT NULL,
    message_id   bigint      NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_ai_delivery_run ON ai_delivery (run_id);

-- Текстовая обратная связь владельца. Это управленческое правило/приоритет,
-- но не источник фактических цифр.
CREATE TABLE IF NOT EXISTS ai_guidance (
    id          bigserial   PRIMARY KEY,
    run_id      text        NOT NULL REFERENCES ai_analysis_run(id) ON DELETE CASCADE,
    chat_id     text        NOT NULL,
    user_id     text        NOT NULL,
    guidance    text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ai_guidance_created ON ai_guidance (created_at DESC);
