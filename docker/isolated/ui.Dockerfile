# The UI front door (`./run.sh --isolated`): nginx without root, config template
# baked in (filled from the environment at start).
FROM nginxinc/nginx-unprivileged:alpine
COPY ui-proxy.conf.template /etc/nginx/templates/default.conf.template
