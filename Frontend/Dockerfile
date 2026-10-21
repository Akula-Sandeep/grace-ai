FROM node:24-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci

ARG VITE_API_URL=/api
ENV VITE_API_URL=${VITE_API_URL}
ARG VITE_SENTRY_DSN=
ENV VITE_SENTRY_DSN=${VITE_SENTRY_DSN}
ARG VITE_SENTRY_RELEASE=
ENV VITE_SENTRY_RELEASE=${VITE_SENTRY_RELEASE}

COPY . .
RUN npm run build

FROM nginx:alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-security-headers.conf /etc/nginx/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html

RUN mkdir -p \
      /var/cache/nginx/client_temp \
      /var/cache/nginx/proxy_temp \
      /var/cache/nginx/fastcgi_temp \
      /var/cache/nginx/uwsgi_temp \
      /var/cache/nginx/scgi_temp \
    && chown -R 101:101 /var/cache/nginx \
    && sed -i \
      -e 's#^pid[[:space:]][[:space:]]*/run/nginx[.]pid;#pid /tmp/nginx.pid;#' \
      -e '/^user[[:space:]]/d' \
      /etc/nginx/nginx.conf

USER 101:101

EXPOSE 80
