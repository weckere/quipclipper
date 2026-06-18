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
    # -b: password from args, -m: apr1/MD5 (broadly supported by nginx), -c: create
    htpasswd -bmc "$htpw" "$user" "$QC_PASSWORD" >/dev/null 2>&1
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
    echo "[quipclipper] QC_PASSWORD not set — auth disabled"
fi
