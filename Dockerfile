# Dockerfile para Shiro Synthesis Two Bot
FROM node:18-alpine AS builder

# Establecer directorio de trabajo
WORKDIR /app

# Copiar archivos de dependencias
COPY package*.json ./

# Instalar todas las dependencias (incluyendo dev para posible compilación)
RUN npm ci

# -----------------------------
# Etapa de producción
FROM node:18-alpine

# Crear usuario no root
RUN addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001

WORKDIR /app

# Copiar dependencias de la etapa builder
COPY --from=builder --chown=nodejs:nodejs /app/node_modules ./node_modules

# Copiar código fuente
COPY --chown=nodejs:nodejs . .

# Exponer puerto (si se usa webhook)
EXPOSE 3000

# Cambiar a usuario no root
USER nodejs

# Comando para iniciar el bot
CMD ["node", "shiro-telegram.js"]
