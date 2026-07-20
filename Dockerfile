# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system --gid 10001 askseoul \
    && adduser --system --uid 10001 --ingroup askseoul \
        --home /app --no-create-home askseoul

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt


FROM base AS test

COPY requirements-dev.txt .
RUN python -m pip install -r requirements-dev.txt

COPY . .

CMD ["python", "-m", "pytest", "-q"]


FROM base AS runtime

COPY --chown=askseoul:askseoul app ./app
COPY --chown=askseoul:askseoul scripts ./scripts
COPY --chown=askseoul:askseoul snapshot ./snapshot

RUN mkdir -p /app/data /app/app/charts/data/cache \
    && chown -R askseoul:askseoul /app/data /app/app/charts/data

USER askseoul

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=12 \
    CMD ["python", "-c", "import http.client, os, sys; c=http.client.HTTPConnection('127.0.0.1',8765,timeout=5); c.request('GET','/health',headers={'Host':os.environ.get('HEALTHCHECK_HOST','127.0.0.1')}); r=c.getresponse(); sys.exit(0 if r.status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
