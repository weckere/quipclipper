#!/bin/sh
# Trust a reverse proxy in front of this nginx to name the client (QC_TRUSTED_PROXIES).
# Runs from nginx's /docker-entrypoint.d/ before nginx starts. For requests from the listed
# addresses or CIDR ranges (comma- or space-separated), nginx takes the client's address
# from QC_REAL_IP_HEADER (default X-Forwarded-For: its last entry, the one the nearest proxy
# added), so the pairing banner's "From" and its per-address rate limit see the real caller.
# Unset: the snippet is empty and the connection's address is the client.
set -e

conf="${QC_REALIP_CONF:-/etc/nginx/quip-realip.conf}"  # overridable for tests
: > "$conf"

if [ -n "${QC_TRUSTED_PROXIES:-}" ]; then
    header="${QC_REAL_IP_HEADER:-X-Forwarded-For}"
    case "$header" in
        ''|*[!A-Za-z0-9-]*) echo "[quipclipper] QC_REAL_IP_HEADER must be a header name; ignoring QC_TRUSTED_PROXIES" >&2; exit 0 ;;
    esac
    for p in $(printf '%s' "$QC_TRUSTED_PROXIES" | tr ',' ' '); do
        # Only addresses and ranges go into nginx's config.
        case "$p" in
            *[!0-9A-Fa-f.:/]*|'') echo "[quipclipper] QC_TRUSTED_PROXIES: '$p' isn't an IP address or CIDR range; skipped" >&2; continue ;;
        esac
        printf 'set_real_ip_from %s;\n' "$p" >> "$conf"
    done
    if [ -s "$conf" ]; then
        printf 'real_ip_header %s;\n' "$header" >> "$conf"
        echo "[quipclipper] client addresses taken from $header for trusted proxies: $QC_TRUSTED_PROXIES"
    fi
fi
