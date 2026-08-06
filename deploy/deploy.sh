#!/bin/bash
# Деплой Hermes на сервер через rsync (вместо ручного scp).
# Запускать из корня репозитория после коммита.
set -e

REMOTE="root@45.139.76.204"
SSH_KEY="$HOME/.ssh/flower-agent-timeweb"
APP="/opt/hermes/app"
VENV="/opt/hermes/venv"

echo "==> Синхронизация кода..."
rsync -az --delete \
      -e "ssh -i $SSH_KEY" \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='.env' \
      --exclude='.git' \
      ./hermes/ $REMOTE:$APP/hermes/

echo "==> Синтаксическая проверка на сервере..."
ssh -i $SSH_KEY $REMOTE "$VENV/bin/python -m py_compile $APP/hermes/*.py"

echo "==> Перезапуск бота..."
ssh -i $SSH_KEY $REMOTE "systemctl restart hermes-bot"

echo "==> Деплой завершён."
