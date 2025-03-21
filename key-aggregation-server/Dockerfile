# 
FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

# 
WORKDIR /code

# 
COPY ./requirements.txt /code/requirements.txt

# 
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# 
COPY ./app /code/app

# 
ENTRYPOINT ["python", "app/main.py"]
