FROM python:3.12-slim AS base

# OS deps:
#   libsqlite3-mod-spatialite — SpatiaLite extension
#   libgl1 / libglib2.0-0    — opencv runtime
#   tesseract-ocr            — used by tools/ocr (loaded lazily)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libsqlite3-mod-spatialite \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*
# Note: rasterio and pyproj wheels each ship their own PROJ binary + CRS
# database. Don't set PROJ_DATA — that forces a single path and the two
# packages may have different schema versions, breaking one of them.

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY tools/ ./tools/
# schemas/ ships with the backend (already included by the COPY above)

# Caller mounts the OFM root at /ofm.
ENV OFM_ROOT=/ofm
ENV OFM_CORS_ORIGINS=*

EXPOSE 8765

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8765"]


# --- test stage ----------------------------------------------------------
FROM base AS test
RUN pip install --no-cache-dir pytest httpx
COPY backend/tests/ ./backend/tests/
ENV PYTHONPATH=/app
CMD ["pytest", "-q", "backend/tests"]
