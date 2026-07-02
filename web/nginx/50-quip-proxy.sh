#!/bin/sh
# Inject a shared secret on every request nginx forwards to the backend, so the
# backend can reject anything that reaches its port directly (bypassing nginx and
# its basic-auth gate — e.g. another container on the same Docker network).
# Runs from nginx's /docker-entrypoint.d/ before nginx starts. When QC_PROXY_SECRET
# is set, write the proxy_set_header snippet that nginx.conf includes in each
# proxied location; otherwise leave it empty (the backend then skips the check).
set -e

conf=/etc/nginx/quip-proxy.conf

if [ -n "${QC_PROXY_SECRET:-}" ]; then
    # The value can contain arbitrary characters; nginx needs it quoted. A double
    # quote or backslash in the secret would break the directive, so escape both.
    escaped=$(printf '%s' "$QC_PROXY_SECRET" | sed 's/[\\"]/\\&/g')
    printf 'proxy_set_header X-Quip-Proxy-Secret "%s";\n' "$escaped" > "$conf"
    echo "[quipclipper] backend proxy secret enabled"
else
    : > "$conf"
    echo "[quipclipper] QC_PROXY_SECRET not set — backend accepts direct requests"
fi
