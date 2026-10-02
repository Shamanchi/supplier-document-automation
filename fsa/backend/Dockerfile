FROM python:3.12-slim

WORKDIR /srv

# 7z нужен для распаковки архивов набора открытых данных, PyMuPDF — для
# чтения текстового слоя PDF до обращения к OCR.
RUN apt-get update \
 && apt-get install -y --no-install-recommends p7zip-full \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY . /srv

EXPOSE 8100
# Keep the database schema in sync on every fresh deployment, just like price-api.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8100"]
