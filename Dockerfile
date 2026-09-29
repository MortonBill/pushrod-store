FROM python:3.12-slim

WORKDIR /app

# Dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code, brand configs, frontend, and bundled catalog data + images
COPY backend/ ./backend/
COPY brands/ ./brands/
COPY frontend/ ./frontend/
COPY data/ ./data/

# Render injects PORT; bind 0.0.0.0 for the platform proxy.
# BRAND=gateway serves the 4-door unified catalog (default).
ENV BRAND=gateway
ENV PYTHONUNBUFFERED=1

CMD exec gunicorn --chdir backend --bind 0.0.0.0:$PORT --workers 2 --timeout 120 app:app
