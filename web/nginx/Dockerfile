# Tiny nginx image with the quipclipper-web frontend + site config baked in, so
# the OMV/standalone compose needs no local files. Build context is the repo
# root (the compose file points at the GitHub URL).
FROM nginx:stable

# apache2-utils provides htpasswd for the optional basic-auth gate (Phase 6);
# curl fetches hls.js below.
# Bake empty quip-auth.conf / quip-proxy.conf so nginx.conf's `include`s work
# even before the entrypoints run / when the features are disabled.
RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && : > /etc/nginx/quip-auth.conf \
    && : > /etc/nginx/quip-proxy.conf

COPY web/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY web/nginx/40-quip-auth.sh /docker-entrypoint.d/40-quip-auth.sh
COPY web/nginx/50-quip-proxy.sh /docker-entrypoint.d/50-quip-proxy.sh
RUN chmod +x /docker-entrypoint.d/40-quip-auth.sh /docker-entrypoint.d/50-quip-proxy.sh
COPY web/frontend /usr/share/nginx/html

# hls.js (desktop Chrome/Firefox playback) isn't in git: web/frontend/vendor/ is
# gitignored and Nix installs it from nix/hls-js.nix. Fetch the same npm tarball,
# pinned by the same hash (tests/test_packaging.py keeps the two in step).
ARG HLS_JS_VERSION=1.6.16
ARG HLS_JS_SHA256=d282339fed09a0987d55b49b41430a67d021861c24d33722e75e5ca2e5179c77
RUN curl -fsSLo /tmp/hls.tgz "https://registry.npmjs.org/hls.js/-/hls.js-${HLS_JS_VERSION}.tgz" \
    && echo "${HLS_JS_SHA256}  /tmp/hls.tgz" | sha256sum -c - \
    && mkdir -p /usr/share/nginx/html/vendor \
    && tar -xzf /tmp/hls.tgz -C /tmp package/dist/hls.min.js package/LICENSE \
    && install -m 444 /tmp/package/dist/hls.min.js /usr/share/nginx/html/vendor/hls.min.js \
    && install -m 444 /tmp/package/LICENSE /usr/share/nginx/html/vendor/hls.min.js.LICENSE \
    && rm -rf /tmp/hls.tgz /tmp/package
