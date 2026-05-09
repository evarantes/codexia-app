FROM node:20-bullseye

WORKDIR /bridge

RUN apt-get update \
  && apt-get install -y chromium \
  && rm -rf /var/lib/apt/lists/*

ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PORT=3030

COPY package.json package-lock.json* ./
RUN npm install --silent

COPY . .

EXPOSE 3030

CMD ["node", "server.js"]

