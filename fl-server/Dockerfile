FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

WORKDIR /app

COPY ./requirements.txt .
COPY ./app .

RUN pip install --no-cache-dir -r requirements.txt

ENTRYPOINT [ "python", "flower_server.py" ]