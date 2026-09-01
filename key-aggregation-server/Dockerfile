FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN apt-get update \
 && apt-get upgrade --yes \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip install --no-cache-dir --upgrade \
      pip==26.2.1 setuptools==84.0.0 wheel==0.48.0
RUN groupadd --system deki && useradd --system --gid deki --home-dir /app deki
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 \
 && pip install --no-cache-dir -r /app/requirements.txt \
 && pip uninstall --yes pip wheel \
 && rm -rf /usr/local/lib/python3.12/ensurepip
COPY app /app/app
RUN mkdir -p /data/artifacts && chown -R deki:deki /data /app
USER deki
# Probes the API served by the default CMD. Override or disable it for
# containers started with a different command, such as the worker.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
