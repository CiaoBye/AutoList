FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt
COPY app ./app

RUN useradd --system --uid 10001 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser
EXPOSE 8080
VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
