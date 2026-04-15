FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py videocutter.html ./
ENV PORT=8765
CMD ["python", "server.py"]
