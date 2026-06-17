# Tiny nginx image with the quipclipper-web frontend + site config baked in, so
# the OMV/standalone compose needs no local files. Build context is the repo
# root (the compose file points at the GitHub URL).
FROM nginx:stable

# apache2-utils provides htpasswd for the optional basic-auth gate (Phase 6).
# Bake an empty quip-auth.conf so nginx.conf's `include` works even before the
# entrypoint runs / when auth is disabled.
RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2-utils \
    && rm -rf /var/lib/apt/lists/* \
    && : > /etc/nginx/quip-auth.conf

COPY web/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY web/nginx/40-quip-auth.sh /docker-entrypoint.d/40-quip-auth.sh
RUN chmod +x /docker-entrypoint.d/40-quip-auth.sh
COPY web/frontend /usr/share/nginx/html
