FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    WEBUI_PORT=8585

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shelfkeeper ./shelfkeeper
COPY config/config.yml ./config.example.yml

VOLUME /config
EXPOSE 8585
ENTRYPOINT ["python", "-m", "shelfkeeper"]
CMD ["--web"]
