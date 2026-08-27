FROM python:3.12-alpine

RUN apk add --no-cache ca-certificates tzdata \
    && adduser -D -H -u 1000 ddns

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/agent.py /app/agent.py

ENV CONFIG_PATH=/config/config.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER ddns

ENTRYPOINT ["python", "/app/agent.py"]
CMD []
