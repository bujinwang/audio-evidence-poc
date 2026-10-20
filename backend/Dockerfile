FROM python:3.12-slim

# FFmpeg is required for probing, working-copy conversion, and clip export.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-asr.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional heavy ASR stack (WhisperX / DeepFilterNet / sentence-transformers).
# Off by default: the image stays small and the app reports the missing
# dependency instead of failing. Build with --build-arg INSTALL_ASR=true.
ARG INSTALL_ASR=false
RUN if [ "$INSTALL_ASR" = "true" ]; then \
      pip install --no-cache-dir -r requirements-asr.txt; \
    fi

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
