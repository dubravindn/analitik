#!/usr/bin/env bash
# Устанавливает/обновляет Hermes на сервере и регистрирует systemd-таймер.
# Запускать от deploy (sudo NOPASSWD):
#   bash install.sh
set -euo pipefail

APP_DIR=/opt/hermes
REPO_DIR=$APP_DIR/app
VENV=$APP_DIR/venv
UNIT_DIR=/etc/systemd/system

echo "=== [1/5] Синхронизация кода ==="
cd "$REPO_DIR"
git pull --ff-only

echo "=== [2/5] Установка зависимостей ==="
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

echo "=== [3/5] Инициализация схемы БД ==="
sudo -u hermes "$VENV/bin/python" -m hermes init-db

echo "=== [4/5] Установка systemd-юнитов ==="
sudo cp "$REPO_DIR/deploy/hermes-daily.service" "$UNIT_DIR/"
sudo cp "$REPO_DIR/deploy/hermes-daily.timer"   "$UNIT_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-daily.timer

echo "=== [5/5] Статус таймера ==="
systemctl status hermes-daily.timer --no-pager

echo ""
echo "✓ Готово. Таймер: sudo systemctl list-timers hermes-daily.timer"
echo "  Ручной запуск отчёта: sudo systemctl start hermes-daily.service"
echo "  Логи: sudo journalctl -u hermes-daily.service -n 50"
