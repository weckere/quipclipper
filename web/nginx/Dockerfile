# Tiny nginx image with the quipclipper-web frontend + site config baked in, so
# the OMV/standalone compose needs no local files. Build context is the repo
# root (the compose file points at the GitHub URL).
FROM nginx:stable

COPY web/nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY web/frontend /usr/share/nginx/html
