FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для сборки C-расширений и проверки здоровья сервиса
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Копирование файлов зависимостей (оба варианта написания для совместимости)
COPY requirements.txt Requierments.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода проекта и вспомогательных модулей
COPY core/ ./core/
COPY max_bot/ ./max_bot/
COPY proxy_server.py ./
COPY entrypoint.sh ./
COPY .env.example ./

# Назначение прав на исполнение entrypoint-скрипта
RUN chmod +x ./entrypoint.sh

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
