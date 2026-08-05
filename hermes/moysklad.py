"""Клиент МойСклад — ТОЛЬКО ЧТЕНИЕ.

Жёсткое ограничение: клиент умеет исключительно HTTP GET. Любой вызов, который
попытался бы изменить данные (POST/PUT/DELETE), технически невозможен — метода нет.
Это гарантия принципа «в МойСклад не пишем» на уровне кода, а не договорённости.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("hermes.moysklad")

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"


class MoyskladError(RuntimeError):
    pass


class MoyskladClient:
    def __init__(self, token: str, timeout: int = 60):
        if not token:
            raise MoyskladError("Пустой токен МойСклад")
        self._token = token
        self._timeout = timeout

    # --- низкий уровень: единственный сетевой метод, только GET ---
    def _get(self, path: str, params: dict | None = None, _retries: int = 3) -> dict:
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Accept-Encoding", "gzip")
        req.add_header("User-Agent", "hermes/0.1 (read-only analytics)")

        for attempt in range(1, _retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return json.loads(data)
            except urllib.error.HTTPError as e:
                body = e.read()[:500].decode("utf-8", "replace")
                # 429 — превышение лимита запросов, ждём и повторяем
                if e.code == 429 and attempt < _retries:
                    wait = 2 * attempt
                    log.warning("МойСклад 429 (лимит), пауза %ss и повтор", wait)
                    time.sleep(wait)
                    continue
                raise MoyskladError(f"HTTP {e.code} на {path}: {body}") from e
            except urllib.error.URLError as e:
                if attempt < _retries:
                    log.warning("Сетевая ошибка %s, повтор %s/%s", e, attempt, _retries)
                    time.sleep(2 * attempt)
                    continue
                raise MoyskladError(f"Сеть недоступна на {path}: {e}") from e
        raise MoyskladError(f"Не удалось получить {path} за {_retries} попыток")

    # --- удобные обёртки ---
    def whoami(self) -> dict:
        return self._get("/context/employee")

    def stores(self) -> list[dict]:
        return self._get("/entity/store", {"limit": 100}).get("rows", [])

    def count(self, entity_path: str, filter_str: str | None = None) -> int:
        """Число документов по фильтру, без выгрузки строк (через meta.size)."""
        params = {"limit": 1}
        if filter_str:
            params["filter"] = filter_str
        return self._get(entity_path, params).get("meta", {}).get("size", 0)

    def profit_by_product(self, store_href: str, moment_from: str, moment_to: str) -> list[dict]:
        """Отчёт «Прибыльность по товарам» с фильтром по складу.

        Возвращает строки с полями sellSum, sellCostSum, returnSum, returnCostSum,
        profit, sellQuantity и assortment. Суммы — в КОПЕЙКАХ.
        """
        rows: list[dict] = []
        offset = 0
        while True:
            page = self._get(
                "/report/profit/byproduct",
                {
                    "momentFrom": moment_from,
                    "momentTo": moment_to,
                    "filter": f"store={store_href}",
                    "limit": 1000,
                    "offset": offset,
                },
            )
            batch = page.get("rows", [])
            rows.extend(batch)
            size = page.get("meta", {}).get("size", 0)
            offset += len(batch)
            if offset >= size or not batch:
                break
        return rows

    def store_href(self, store_id: str) -> str:
        return f"{BASE_URL}/entity/store/{store_id}"
