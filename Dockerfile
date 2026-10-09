FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src
COPY pyproject.toml ./
RUN pip install --no-deps -e .

# The page needs nothing fetched at build time: the response-time chart is
# drawn by the server as inline SVG, so there is no chart library to vendor.

EXPOSE 8081

CMD ["uvicorn", "status_service.main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log"]
