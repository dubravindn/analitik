import type { Metadata } from "next";
import { formatDelta, formatRubles, getDashboardSnapshot } from "../lib/dashboard-data";

export const metadata: Metadata = {
  title: "ЦБД — управленческий дашборд",
  description: "Продажи, прибыль, расходы и контроль бизнеса в одном окне.",
};

export const dynamic = "force-dynamic";

function shortDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : "—";
}

export default async function Home() {
  const { snapshot, isLive } = await getDashboardSnapshot();
  const { metrics } = snapshot;
  const maxStoreRevenue = Math.max(...snapshot.stores.map((store) => store.revenueKop), 1);
  const stores = snapshot.stores.map((store) => ({
    ...store,
    value: formatRubles(store.revenueKop),
    width: `${Math.max(8, Math.round(store.revenueKop / maxStoreRevenue * 100))}%`,
    tone: store.name === "База Воровского 107/1" ? "base" : "retail",
  }));
  const comparisonRows = snapshot.comparison ? [
    { name: "Выручка", current: metrics.revenueKop, previous: snapshot.comparison.metrics.revenueKop, goodUp: true },
    { name: "Валовая прибыль", current: metrics.grossProfitKop, previous: snapshot.comparison.metrics.grossProfitKop, goodUp: true },
    { name: "После списаний", current: metrics.profitAfterWriteoffsKop, previous: snapshot.comparison.metrics.profitAfterWriteoffsKop, goodUp: true },
    { name: "Списания", current: metrics.writeoffsKop, previous: snapshot.comparison.metrics.writeoffsKop, goodUp: false },
    { name: "Операционные расходы", current: metrics.operatingExpensesKop, previous: snapshot.comparison.metrics.operatingExpensesKop, goodUp: false },
  ] : [];
  return (
    <main className="app-shell">
      <aside className="sidebar" aria-label="Разделы дашборда">
        <div className="brand">
          <span className="brand-mark">ЦБД</span>
          <span className="brand-subtitle">Цветочная База<br />Дубравиных</span>
        </div>
        <nav className="nav-list">
          <a className="nav-item active" href="#overview"><span>⌂</span>Главная</a>
          <a className="nav-item" href="#clients"><span>◎</span>B2B-клиенты</a>
          <a className="nav-item" href="#orders"><span>□</span>Заказы и закупка</a>
          <a className="nav-item" href="#control"><span>◇</span>Контроль</a>
        </nav>
        <div className="sidebar-note">
          <span className="status-dot" /> Только чтение
          <small>Дашборд ничего не меняет в МойСклад</small>
        </div>
      </aside>

      <section className="workspace" id="overview">
        <header className="topbar">
          <div>
            <p className="eyebrow">Управленческий центр</p>
            <h1>Картина бизнеса</h1>
          </div>
          <div className="sync-status">
            <span className="status-dot" /> {isLive ? "Данные обновлены" : "Данные подготовлены"}
            <small>{isLive ? "защищённая копия из аналитика" : "контрольный макет · этап 1"}</small>
          </div>
        </header>

        {!isLive && (
          <div className="preview-warning">
            <strong>Контрольный макет</strong>
            <span>Показана структура на сверенных цифрах отчёта 16–22 августа. Подключение живой базы — следующий этап.</span>
          </div>
        )}

        <div className="filter-row" aria-label="Период отчёта">
          <div className="period-title">
            <span>Период</span>
            <strong>{snapshot.period.label}</strong>
          </div>
          <div className="period-pills">
            <button>День</button>
            <button className="selected">Неделя</button>
            <button>Месяц</button>
            <button>Свой период</button>
          </div>
        </div>

        <section className="kpi-grid" aria-label="Ключевые показатели">
          <article className="kpi-card primary">
            <p>Выручка</p><strong>{formatRubles(metrics.revenueKop)}</strong><span>все собственные точки</span>
          </article>
          <article className="kpi-card">
            <p>Валовая прибыль</p><strong>{formatRubles(metrics.grossProfitKop)}</strong><span>выручка − себестоимость</span>
          </article>
          <article className="kpi-card">
            <p>Прибыль после списаний</p><strong>{formatRubles(metrics.profitAfterWriteoffsKop)}</strong><span>после обычной порчи</span>
          </article>
          <article className="kpi-card alert">
            <p>Списания</p><strong>{formatRubles(metrics.writeoffsKop)}</strong><span>без инвентаризационных корректировок</span>
          </article>
        </section>

        <section className="dashboard-grid">
          <article className="panel revenue-panel">
            <div className="panel-heading">
              <div><p className="eyebrow">Продажи</p><h2>Выручка по точкам</h2></div>
              <button className="text-button">Открыть детали →</button>
            </div>
            <div className="store-bars">
              {stores.map((store) => (
                <div className="store-row" key={store.name}>
                  <div className="store-label"><span>{store.name}</span><strong>{store.value}</strong></div>
                  <div className="bar-track"><div className={`bar ${store.tone}`} style={{ width: store.width }} /></div>
                </div>
              ))}
            </div>
          </article>

          <article className="panel focus-panel">
            <div className="panel-heading">
              <div><p className="eyebrow">Фокус руководителя</p><h2>Что требует внимания</h2></div>
            </div>
            <div className="focus-list">
              <div className="focus-item"><span className="focus-icon terra">!</span><div><strong>Сверить классификацию потерь</strong><p>«Списание» и «Возврат» БАЗЫ должны уменьшать прибыль один раз.</p></div></div>
              <div className="focus-item"><span className="focus-icon sage">✓</span><div><strong>Ресторанные склады исключены</strong><p>ФАБРИКА и СОБРАНИЕ учитываются только в перемещениях.</p></div></div>
              <div className="focus-item muted"><span className="focus-icon">AI</span><div><strong>Краткая сводка ИИ</strong><p>Появится после подключения живых данных и контрольной сверки.</p></div></div>
            </div>
          </article>
        </section>

        <section className="finance-grid">
          <article className="panel profit-bridge">
            <div className="panel-heading">
              <div><p className="eyebrow">Формула без скрытых вычетов</p><h2>Из чего получилась прибыль</h2></div>
            </div>
            <div className="bridge-row">
              <div><span>Валовая прибыль</span><strong>{formatRubles(metrics.grossProfitKop)}</strong></div>
              <b>−</b>
              <div><span>Операционные расходы</span><strong>{formatRubles(metrics.operatingExpensesKop)}</strong></div>
              <b>−</b>
              <div><span>Списания</span><strong>{formatRubles(metrics.writeoffsKop)}</strong></div>
              <b>=</b>
              <div className="bridge-result"><span>После списаний</span><strong>{formatRubles(metrics.profitAfterWriteoffsKop)}</strong></div>
            </div>
            <p className="formula-note">«Списание» и «Возврат» БАЗЫ перенесены из расходов в списания и учитываются один раз. Инвентаризационные корректировки сюда не входят.</p>
          </article>

          <article className="panel comparison-panel">
            <div className="panel-heading">
              <div><p className="eyebrow">Динамика</p><h2>К предыдущему периоду</h2></div>
            </div>
            {snapshot.comparison ? (
              <div className="comparison-list">
                {comparisonRows.map((row) => {
                  const grew = row.current >= row.previous;
                  const favorable = row.goodUp ? grew : !grew;
                  return (
                    <div className="comparison-row" key={row.name}>
                      <span>{row.name}</span>
                      <strong>{formatRubles(row.current)}</strong>
                      <b className={favorable ? "delta-good" : "delta-alert"}>{formatDelta(row.current, row.previous)}</b>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="empty-copy">Появится после первой загрузки живого периода. Сравниваются два соседних периода одинаковой длины.</p>
            )}
          </article>
        </section>

        <section className="clients-section" id="clients">
          <div className="section-heading">
            <div><p className="eyebrow">Только склад БАЗА</p><h2>B2B-клиенты</h2></div>
            <p>Розничные заглушки и внутренние контрагенты исключены.</p>
          </div>
          {snapshot.clients ? (
            <>
              <div className="client-kpis">
                <article><span>Клиентов за период</span><strong>{snapshot.clients.summary.clients}</strong></article>
                <article><span>Отгрузок</span><strong>{snapshot.clients.summary.orders}</strong></article>
                <article><span>Выручка B2B</span><strong>{formatRubles(snapshot.clients.summary.revenueKop)}</strong></article>
                <article className="client-alert"><span>Возможный отток</span><strong>{snapshot.clients.churn.length}</strong><small>&gt;10 дней · средний чек от 10 000 ₽</small></article>
              </div>
              <div className="clients-grid">
                <article className="panel compact-panel">
                  <div className="panel-heading"><div><p className="eyebrow">Выручка</p><h2>Топ клиентов БАЗЫ</h2></div></div>
                  <div className="data-list">
                    {snapshot.clients.top.map((client, index) => (
                      <div className="data-row" key={`${client.name}-${index}`}>
                        <b>{index + 1}</b><span>{client.name}</span><small>{client.orders} отгр.</small><strong>{formatRubles(client.revenueKop)}</strong>
                      </div>
                    ))}
                  </div>
                </article>
                <article className="panel compact-panel">
                  <div className="panel-heading"><div><p className="eyebrow">Контроль возврата</p><h2>Клиенты без заказа</h2></div></div>
                  <div className="data-list">
                    {snapshot.clients.churn.slice(0, 12).map((client, index) => (
                      <div className="data-row churn-row" key={`${client.name}-${index}`}>
                        <b>!</b><span>{client.name}</span><small>{client.daysSince} дн. · ср. {formatRubles(client.averageCheckKop)}</small><strong>{client.lastOrder}</strong>
                      </div>
                    ))}
                    {!snapshot.clients.churn.length && <p className="empty-copy">Клиентов, подходящих под правило оттока, нет.</p>}
                  </div>
                </article>
              </div>
            </>
          ) : (
            <div className="preview-warning"><strong>Следующая загрузка</strong><span>Клиентский блок появится после обновления production-снимка.</span></div>
          )}
        </section>

        <section className="clients-section" id="orders">
          <div className="section-heading">
            <div><p className="eyebrow">Текущее состояние · БАЗА</p><h2>Дебиторка и незакрытые заказы</h2></div>
            <p>Это текущие данные МойСклад на момент загрузки, а не итог выбранного периода.</p>
          </div>
          {snapshot.liveB2b?.available && snapshot.liveB2b.receivables && snapshot.liveB2b.openOrders ? (
            <>
              <div className="client-kpis order-kpis">
                <article className="client-alert"><span>Нам должны</span><strong>{formatRubles(snapshot.liveB2b.receivables.totalKop)}</strong><small>{snapshot.liveB2b.receivables.clients} контрагентов с положительным балансом</small></article>
                <article><span>Незакрытых заказов</span><strong>{snapshot.liveB2b.openOrders.orders}</strong></article>
                <article><span>Осталось отгрузить</span><strong>{formatRubles(snapshot.liveB2b.openOrders.remainingToShipKop)}</strong></article>
                <article><span>Не оплачено по заказам</span><strong>{formatRubles(snapshot.liveB2b.openOrders.unpaidKop)}</strong></article>
              </div>
              <div className="clients-grid">
                <article className="panel compact-panel">
                  <div className="panel-heading"><div><p className="eyebrow">Положительный баланс</p><h2>Крупная дебиторка</h2></div></div>
                  <div className="data-list">
                    {snapshot.liveB2b.receivables.rows.slice(0, 12).map((debtor, index) => (
                      <div className="data-row" key={`${debtor.name}-${index}`}>
                        <b>{index + 1}</b><span>{debtor.name}</span><small>посл. {shortDate(debtor.lastDemandDate)}</small><strong>{formatRubles(debtor.balanceKop)}</strong>
                      </div>
                    ))}
                    {!snapshot.liveB2b.receivables.rows.length && <p className="empty-copy">Положительной дебиторской задолженности не найдено.</p>}
                  </div>
                  <p className="formula-note">Правило: {snapshot.liveB2b.receivables.rule}. Перед управленческими решениями знак баланса сверяется с интерфейсом МойСклад.</p>
                </article>
                <article className="panel compact-panel">
                  <div className="panel-heading"><div><p className="eyebrow">Не отгружено полностью</p><h2>Незакрытые заказы</h2></div></div>
                  <div className="data-list">
                    {snapshot.liveB2b.openOrders.rows.slice(0, 12).map((order, index) => (
                      <div className="data-row order-row" key={`${order.number}-${index}`}>
                        <b>№</b><span>{order.number} · {order.client}</span><small>{shortDate(order.moment)} · {order.state}</small><strong>{formatRubles(order.remainingToShipKop)}</strong>
                      </div>
                    ))}
                    {!snapshot.liveB2b.openOrders.rows.length && <p className="empty-copy">Незакрытых заказов БАЗЫ не найдено.</p>}
                  </div>
                </article>
              </div>
            </>
          ) : (
            <div className="preview-warning"><strong>Данные временно недоступны</strong><span>Продажи и прибыль продолжают работать; read-only запрос текущих заказов будет повторён при следующем обновлении.</span></div>
          )}
        </section>

        <section className="clients-section ai-section" id="ai">
          <div className="section-heading">
            <div><p className="eyebrow">Трезвый взгляд со стороны</p><h2>Короткая сводка ИИ</h2></div>
            <p>Показывается только уже проверенный ответ действующего ИИ-аналитика. Цифры берутся из фактов отчёта.</p>
          </div>
          {snapshot.aiSummary?.available ? (
            <article className="panel ai-panel">
              <div className="ai-findings">
                {(snapshot.aiSummary.findings ?? []).map((finding, index) => (
                  <div className="ai-finding" key={`${finding.title}-${index}`}>
                    <span className={`severity severity-${finding.severity}`}>{finding.severity === "critical" ? "Критично" : finding.severity === "warning" ? "Внимание" : "Наблюдение"}</span>
                    <div><h3>{finding.title}</h3><p><b>Факт:</b> {finding.evidence}</p><p><b>Почему важно:</b> {finding.whyItMatters}</p><p className="ai-action"><b>Что сделать:</b> {finding.action}</p></div>
                  </div>
                ))}
                {!(snapshot.aiSummary.findings ?? []).length && <p className="empty-copy">Существенных отклонений по переданным фактам не найдено.</p>}
              </div>
              <footer className="ai-footer">Источник: {snapshot.aiSummary.reportId} · ИИ ничего не меняет в МойСклад и не оформляет заказы.</footer>
            </article>
          ) : (
            <div className="preview-warning"><strong>Ожидается сводка</strong><span>Блок появится после следующего проверенного ежедневного или периодического анализа.</span></div>
          )}
        </section>

        <section className="coming-grid">
          <article className="coming-card" id="control"><span>04</span><div><h3>Контроль операций</h3><p>Правки задним числом, инвентаризации и аномалии.</p></div><b>после заказов</b></article>
        </section>
      </section>
    </main>
  );
}
