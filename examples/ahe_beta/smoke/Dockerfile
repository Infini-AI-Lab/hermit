FROM node:22-bookworm-slim
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
RUN sed -i "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends python3 curl ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@0.164.0
WORKDIR /app
