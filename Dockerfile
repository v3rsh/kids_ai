FROM python:3.10-slim

WORKDIR /app

# Установка системных зависимостей и настройка времени
RUN apt-get update && apt-get install -y \
    git \
    ntpsec-ntpdate \
    tzdata \
    postgresql-client \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Установка часового пояса по умолчанию
ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Установка Python зависимостей
RUN pip install --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода приложения
COPY app/ .

# Создание директорий для данных, логов и бэкапов
RUN mkdir -p /app/data /app/logs /app/backup

# Порт для веб-сервера
EXPOSE 8000

# Количество workers (по умолчанию 1 для обратной совместимости)
ENV UVICORN_WORKERS=1

# Запуск через uvicorn (кол-во workers из env)
CMD uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS}
