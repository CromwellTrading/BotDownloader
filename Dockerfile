FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema necesarias para yt-dlp y ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py webapp.html ./

# Puerto que usará Render
EXPOSE 8080

CMD ["python", "main.py"]
