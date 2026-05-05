FROM python:3.12-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for vendoring third-party JS at build time. Avoids
# runtime CDN calls — the page works even if jsdelivr is unreachable.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src
COPY pyproject.toml ./
RUN pip install --no-deps -e .

# Vendor ECharts at build time — pinned version. The page works even if
# jsdelivr is later unreachable. To enable integrity verification, run:
#   curl -fsSL https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js | sha256sum
# and uncomment the sha256sum check below with the printed hash.
RUN curl -fsSL -o /app/src/status_service/static/echarts.min.js \
    https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js
# RUN echo "<sha256>  /app/src/status_service/static/echarts.min.js" | sha256sum -c -

# Final stage: drop curl and reinstall as non-root-friendly. Single-stage
# is fine here since the vendored asset is just a static file.
EXPOSE 8081

CMD ["uvicorn", "status_service.main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log"]
