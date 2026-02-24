# Usamos una imagen base con Python y Node (slim para ahorrar espacio)
FROM python:3.11-slim

# Instalamos Node.js 18 (necesario para compilar el proveedor)
RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Instalamos dependencias del sistema para yt-dlp y compilación
RUN apt-get update && apt-get install -y ffmpeg git && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Establecemos el directorio de trabajo
WORKDIR /app

# Copiamos el código del bot
COPY main.py webapp.html requirements.txt ./

# Instalamos dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Clonamos y compilamos el proveedor de PO tokens
RUN git clone --single-branch --branch 1.2.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /bgutil
WORKDIR /bgutil/server
RUN npm install && npx tsc

WORKDIR /app

# Script de entrada: lanza proveedor en background y luego el bot (principal)
RUN echo '#!/bin/bash\n\
# Iniciar proveedor de PO tokens en segundo plano\n\
node /bgutil/server/build/main.js > /var/log/bgutil.log 2>&1 &\n\
# Esperar a que el proveedor esté listo\n\
sleep 5\n\
# Ejecutar el bot (Python)\n\
python main.py\n' > /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8080 4416
CMD ["/entrypoint.sh"]
