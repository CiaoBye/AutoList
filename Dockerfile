ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG AUTOLIST_ICON_URL=https://raw.githubusercontent.com/CiaoBye/AutoList/8a704fe03e381518b190a2d6c015e20a0ffce67f/unraid/autolist-icon.png

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check --timeout 120 --retries 5 \
    --index-url "${PIP_INDEX_URL}" \
    -r requirements.txt
COPY app ./app
# Keep the Unraid tile icon inside the image as well as the web app's SVG logo.
COPY unraid/autolist-icon.png ./app/static/autolist-icon.png
RUN test -s ./app/static/logo.svg \
    && test -s ./app/static/favicon.svg \
    && test -s ./app/static/autolist-icon.png

LABEL net.unraid.docker.managed="dockerman" \
      net.unraid.docker.icon="${AUTOLIST_ICON_URL}"

RUN useradd --system --uid 10001 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser
EXPOSE 8080
VOLUME ["/data"]

# Checks the local process and SQLite file via app/healthcheck.py. It deliberately
# does not call TMDB, PT sites, Emby, Transmission, or MoviePilot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-m", "app.healthcheck"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
