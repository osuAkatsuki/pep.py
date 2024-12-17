#!/usr/bin/env make

build:
	docker build -t bancho-service:latest .

run-api:
	docker run \
		--env APP_COMPONENT=api \
		--network=host \
		--env-file=.env \
		-it bancho-service:latest

run-api-bg:
	docker run \
		--env APP_COMPONENT=api \
		--network=host \
		--env-file=.env \
		-d bancho-service:latest

run-pubsub-handler:
	docker run \
		--env APP_COMPONENT=consume-pubsub-events \
		--network=host \
		--env-file=.env \
		-it bancho-service:latest

run-pubsub-handler-bg:
	docker run \
		--env APP_COMPONENT=consume-pubsub-events \
		--network=host \
		--env-file=.env \
		-d bancho-service:latest python3 -m bancho.pubsub_handler

run-inactive-user-timeout:
	docker run \
		--env APP_COMPONENT=timeout-inactive-tokens \
		--network=host \
		--env-file=.env \
		-it bancho-service:latest python3 -m bancho.inactive_user_timeout

run-inactive-user-timeout-bg:
	docker run \
		--env APP_COMPONENT=timeout-inactive-tokens \
		--network=host \
		--env-file=.env \
		-d bancho-service:latest python3 -m bancho.inactive_user_timeout

run-spammer-silence-cron:
	docker run \
		--env APP_COMPONENT=reset-all-tokens-spam-rate \
		--network=host \
		--env-file=.env \
		-it bancho-service:latest python3 -m bancho.spammer_silence_cron

run-spammer-silence-cron-bg:
	docker run \
		--env APP_COMPONENT=reset-all-tokens-spam-rate \
		--network=host \
		--env-file=.env \
		-d bancho-service:latest python3 -m bancho.spammer_silence_cron

run-irc-handler:
	docker run \
		--env APP_COMPONENT=handle-irc-connections \
		-p "6667:6667" \
		--env-file=.env \
		-it bancho-service:latest
