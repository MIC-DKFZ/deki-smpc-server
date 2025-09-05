FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /code/requirements.txt

COPY ./app /code/app

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/code
ENTRYPOINT ["python", "app/main.py"]
