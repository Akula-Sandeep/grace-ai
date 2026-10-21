#!/bin/sh
set -eu

: "${METRICS_BEARER_TOKEN:?Set METRICS_BEARER_TOKEN in the deployment environment}"
: "${ALERT_WEBHOOK_URL:?Set ALERT_WEBHOOK_URL to the team HTTPS alert receiver}"
case "$ALERT_WEBHOOK_URL" in
  https://*) ;;
  *) echo "ALERT_WEBHOOK_URL must use HTTPS." >&2; exit 2 ;;
esac
mkdir -p ops/secrets
umask 077
printf '%s' "$METRICS_BEARER_TOKEN" > ops/secrets/metrics-token
printf '%s' "$ALERT_WEBHOOK_URL" > ops/secrets/alert-webhook-url
