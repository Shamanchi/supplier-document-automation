FROM python:3.12-slim

WORKDIR /srv

RUN apt-get update \
    && apt-get install --no-install-recommends -y libreoffice-writer \
# Шаг миграции 04: OCR-стек extraction фазы 3 — tesseract вызывается
# subprocess'ом (providers.py), pdftoppm растрирует страницы PDF.
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seeds ./seeds
COPY alembic ./alembic
COPY alembic.ini .

EXPOSE 8000
# Schema is Alembic-managed: apply migrations, then start the API.
# The worker container overrides CMD and does not run migrations.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
