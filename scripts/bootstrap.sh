#!/usr/bin/env bash
set -eo pipefail

cd /srv/root

if [ -z "$APP_ENV" ]; then
  echo "Please set APP_ENV"
  exit 1
fi

if [ -z "$APP_COMPONENT" ]; then
  echo "Please set APP_COMPONENT"
  exit 1
fi

if [[ $PULL_SECRETS_FROM_VAULT -eq 1 ]]; then
  akatsuki vault get bancho-service $APP_ENV -o .env
  source .env
fi

# await database availability
/scripts/await-service.sh $DB_HOST $DB_PORT $SERVICE_READINESS_TIMEOUT

# await redis availability
/scripts/await-service.sh $REDIS_HOST $REDIS_PORT $SERVICE_READINESS_TIMEOUT

if [[ $APP_COMPONENT == "api" ]]; then
  exec /scripts/run-api.sh
elif [[ $APP_COMPONENT == "reset-all-tokens-spam-rate" ]]; then
  exec /scripts/run-reset-all-tokens-spam-rate.sh
elif [[ $APP_COMPONENT == "timeout-inactive-tokens" ]]; then
  exec /scripts/run-timeout-inactive-tokens.sh
elif [[ $APP_COMPONENT == "consume-pubsub-events" ]]; then
  exec /scripts/run-consume-pubsub-events.sh
elif [[ $APP_COMPONENT == "trim-outdated-streams" ]]; then
  exec /scripts/run-trim-outdated-streams.sh
elif [[ $APP_COMPONENT == "handle-irc-connections" ]]; then
  exec /scripts/run-handle-irc-connections.sh
else
  echo "Unknown APP_COMPONENT: $APP_COMPONENT"
  exit 1
fi
