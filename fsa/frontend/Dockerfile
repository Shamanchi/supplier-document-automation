FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org/
# npm registry downloads can occasionally outlive Docker's default retry window.
RUN npm config set registry "${NPM_REGISTRY}" \
 && npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000 \
 && npm config set fetch-timeout 300000 \
 && npm ci
COPY . .
# Путь, под которым интерфейс отдаётся снаружи. По умолчанию корень, чтобы
# локальная сборка и dev-режим работали без параметров.
ARG VITE_BASE_PATH=/
ENV VITE_BASE_PATH=${VITE_BASE_PATH}
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
