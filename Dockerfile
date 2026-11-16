FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN if [ ! -f config/config.yml ] && [ -f config/config.example.yml ]; then cp config/config.example.yml config/config.yml; fi

CMD ["python", "run.py"]
