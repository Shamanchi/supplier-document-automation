"""Единый конфиг подключения stage-2 скриптов к БД price_loader (сервис price-db).

Параметры читаются из переменных окружения PRICE_DB_HOST/PORT/USER/PASSWORD/NAME;
дефолты соответствуют docker-compose.yml (порт 55433 опубликован наружу).
"""
import os

DB_CONFIG = {
    "host": os.environ.get("PRICE_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PRICE_DB_PORT", "55433")),
    "user": os.environ.get("PRICE_DB_USER", "price"),
    "password": os.environ.get("PRICE_DB_PASSWORD", "price"),
    "dbname": os.environ.get("PRICE_DB_NAME", "price_loader"),
}
