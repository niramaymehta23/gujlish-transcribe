# syntax=docker/dockerfile:1.7
# Hosted web app for Google Cloud Run (GPU). Model weights are baked in so a cold start never downloads them.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/models
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt "gunicorn>=23,<24"

# Which models the site offers (comma-separated: flex, gujlish). Flex is gated: the build needs a Hugging Face token
# (passed as the hf_token build secret), and hosting it for other people needs Bodhan AI's approval under its license.
ARG MODELS=gujlish
# Flex runs Python code from its model repository, so pin a reviewed commit.
ARG FLEX_REVISION=ecba9bac9013c0340fe814b336897ee51ed80be9
ENV GUJLISH_MODELS=${MODELS} GUJLISH_FLEX_REVISION=${FLEX_REVISION}
COPY deploy/fetch_models.py deploy/fetch_models.py
RUN --mount=type=secret,id=hf_token,required=false python deploy/fetch_models.py
ENV HF_HUB_OFFLINE=1

COPY . .
ENV GUJLISH_HOSTED=1 GUJLISH_DATA_DIR=/data PORT=8080
# One worker process: jobs are tracked in memory. Threads serve uploads and progress polling meanwhile.
CMD exec gunicorn --workers 1 --threads 16 --timeout 0 --bind "0.0.0.0:${PORT}" "web.server:create_app()"
