FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /requirements.txt
COPY evidencedesk /app/evidencedesk
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-m", "evidencedesk.ingestion.parser_entry"]
