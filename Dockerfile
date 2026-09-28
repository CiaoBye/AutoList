ARG PYTHON_IMAGE=python:3.12-slim
ARG NODE_IMAGE=node:22-alpine

# “电影藏馆”界面在独立阶段构建，运行时镜像不包含 Node。
FROM ${NODE_IMAGE} AS frontend
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --registry "${NPM_REGISTRY}" --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check --timeout 120 --retries 5 \
    --index-url "${PIP_INDEX_URL}" \
    -r requirements.txt
COPY app ./app
COPY --from=frontend /build/app/static/ui ./app/static/ui
RUN test -s ./app/static/logo.svg \
    && test -s ./app/static/favicon.svg \
    && test -s ./app/static/autolist-icon.png \
    && test -s ./app/static/ui/index.html

RUN useradd --system --uid 10001 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser
EXPOSE 8080
VOLUME ["/data"]

# Checks the local process and SQLite file via app/healthcheck.py. It deliberately
# does not call TMDB, PT sites, Emby, Transmission, or MoviePilot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-m", "app.healthcheck"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
