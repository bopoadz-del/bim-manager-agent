# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# libGL and friends: trimesh and ifcopenshell pull in geometry libraries that
# expect them present even when nothing is rendered.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libgomp1 curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY vendor ./vendor
COPY migrations ./migrations
COPY scripts ./scripts
COPY ui ./ui
COPY alembic.ini VENDOR.lock ./

# The pin is verified at build time. An image whose vendored kit does not match
# VENDOR.lock is an image nobody can say what judged their model.
RUN python scripts/vendor_kit.py --check

# The build sha is baked in so /health can report exactly what is deployed.
ARG BUILD_SHA=unknown
ENV MEPJ_BUILD_SHA=${BUILD_SHA}

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# Migrations ship with the code and run on boot. A revision the code does not
# carry fails the deploy loudly instead of quietly serving the wrong schema.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
