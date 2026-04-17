FROM python:3.12-slim
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-dejavu-core \
    fonts-liberation \
    fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# faster-whisper 1.0.3 imports `requests` but doesn't declare it — install manually
RUN pip install --no-cache-dir requests==2.32.3 faster-whisper==1.0.3
# Pré-baixa o modelo tiny (≈75MB) no build pra evitar download em runtime
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"
COPY server.py videocutter.html ./
ENV PORT=8765
CMD ["python", "server.py"]
