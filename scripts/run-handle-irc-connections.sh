#!/usr/bin/env bash
set -eo pipefail

exec python3 workers/daemons/handle_irc_connections.py
