FROM python:3.12-slim

ARG BUILD_VERSION=dev
ENV EVAUTH_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/evauth
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]
