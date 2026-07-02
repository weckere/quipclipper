#!/bin/sh
# Generate the optional HTTP basic-auth gate for quipclipper-web (Phase 6).
# Runs from nginx's /docker-entrypoint.d/ before nginx starts. When QC_PASSWORD
# is set, write an htpasswd file + the auth_basic snippet that nginx.conf
# includes; otherwise leave the snippet empty so the site is open.
set -e

conf=/etc/nginx/quip-auth.conf
htpw=/etc/nginx/.htpasswd

if [ -n "${QC_PASSWORD:-}" ]; then
    user="${QC_USERNAME:-quip}"
    # -i: read password from stdin (keeps it off the process command line, unlike
    # -b), -m: apr1/MD5 (broadly supported by nginx), -c: create.
    printf '%s' "$QC_PASSWORD" | htpasswd -imc "$htpw" "$user" >/dev/null 2>&1
    # When an API token is configured, also add an "api" user whose password is
    # the token, so a programmatic client can pass the gate with one secret:
    # `curl -u api:<token>`. (The backend then recognises the token and skips the
    # CSRF-header requirement.) Uses the FIRST token if several are set; the rest
    # still work via X-API-Key / Bearer when the backend is reached directly.
    if [ -n "${QC_API_TOKEN:-}" ]; then
        token=$(printf '%s' "$QC_API_TOKEN" | cut -d, -f1)
        printf '%s' "$token" | htpasswd -im "$htpw" api >/dev/null 2>&1
        echo "[quipclipper] API token accepted as basic-auth user 'api'"
    fi
    # nginx workers run as the 'nginx' user, so the file must be readable by them
    # (root-only 600 makes credential checks fail with 500). Keep it off world.
    chown root:nginx "$htpw" 2>/dev/null || true
    chmod 640 "$htpw"
    {
        echo 'auth_basic "quipclipper";'
        echo "auth_basic_user_file $htpw;"
    } > "$conf"
    echo "[quipclipper] HTTP basic auth enabled (user: $user)"
else
    : > "$conf"
    # No basic-auth gate: the site is open, so a programmatic client just sends
    # its token (X-API-Key / Bearer) straight through; the backend validates it.
    echo "[quipclipper] QC_PASSWORD not set — auth disabled"
fi
