# Imagen de producción (Render). Incluye Tesseract + Poppler para la lectura de remitos (OCR).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_THREAD_LIMIT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# La aplicación no necesita privilegios de root.
RUN useradd --system --create-home --uid 10001 app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app . .
USER app

# Las migraciones corren al iniciar la app (app.main:lifespan) con un bloqueo en PostgreSQL.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
