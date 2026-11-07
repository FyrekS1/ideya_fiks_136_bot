#!/bin/bash
set -e

# Если передана пользовательская команда, выполняем её напрямую
if [ "$#" -gt 0 ] && [ "$1" != "./entrypoint.sh" ]; then
    exec "$@"
fi

echo "=========================================================="
echo "  Школа.Синхрон (Лицей № 44) — Запуск сервисов в Docker"
echo "=========================================================="

# 1. Фоновый запуск локального OpenAI-совместимого прокси-шлюза
echo "[1/2] Запуск прокси-шлюза (порт 8000)..."
python proxy_server.py &
PROXY_PID=$!

# Ожидание готовности локального шлюза
for i in {1..20}; do
    if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
        echo "[1/2] Прокси-шлюз успешно запущен и готов к работе."
        break
    fi
    sleep 0.5
done

# Корректная обработка сигналов завершения контейнера
cleanup() {
    echo "Завершение работы контейнера..."
    kill -TERM "$PROXY_PID" 2>/dev/null || true
    kill -TERM "$BOT_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# 2. Запуск основного чат-бота платформы MAX
echo "[2/2] Запуск чат-бота MAX Messenger (@ideya_fiks_136_bot)..."
python -m max_bot.main_bot &
BOT_PID=$!

# Ожидание завершения работы сервисов
wait -n "$BOT_PID" "$PROXY_PID"
cleanup
