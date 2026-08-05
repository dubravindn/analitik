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
sudo cp "$REPO_DIR/deploy/hermes-daily.service"  "$UNIT_DIR/"
sudo cp "$REPO_DIR/deploy/hermes-daily.timer"    "$UNIT_DIR/"
sudo cp "$REPO_DIR/deploy/hermes-backup.service" "$UNIT_DIR/"
sudo cp "$REPO_DIR/deploy/hermes-backup.timer"   "$UNIT_DIR/"
# Каталог для бэкапов (владелец — hermes, от него бежит бэкап-сервис).
sudo mkdir -p "$APP_DIR/backups"
sudo chown hermes:hermes "$APP_DIR/backups"
sudo chmod +x "$REPO_DIR/deploy/hermes-backup.sh"
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-daily.timer
sudo systemctl enable --now hermes-backup.timer

echo "=== [5/5] Статус таймеров ==="
systemctl status hermes-daily.timer  --no-pager || true
systemctl status hermes-backup.timer --no-pager || true

echo ""
echo "✓ Готово. Таймеры: sudo systemctl list-timers 'hermes-*'"
echo "  Ручной отчёт: sudo systemctl start hermes-daily.service"
echo "  Ручной бэкап: sudo systemctl start hermes-backup.service"
echo "  Логи: sudo journalctl -u hermes-daily.service -n 50"
