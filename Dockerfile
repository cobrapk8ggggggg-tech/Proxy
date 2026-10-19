# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.txt; else pip install --no-cache-dir -r requirements.txt; fi \
    && useradd --create-home --uid 10001 aurum && mkdir /data && chown aurum:aurum /data
COPY --chown=aurum:aurum qwen_proxy.py ./
COPY --chown=aurum:aurum proxy ./proxy
COPY --chown=aurum:aurum static ./static
USER aurum
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn qwen_proxy:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
