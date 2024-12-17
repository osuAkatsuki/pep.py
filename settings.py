from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def read_bool(val: str) -> bool:
    return val.lower() in ("true", "1")


APP_ENV = os.environ["APP_ENV"]
APP_COMPONENT = os.environ["APP_COMPONENT"]
APP_PORT = int(os.environ["APP_PORT"])
APP_GZIP = read_bool(os.environ["APP_GZIP"])
APP_GZIP_LEVEL = int(os.environ["APP_GZIP_LEVEL"])
APP_CI_KEY = os.environ["APP_CI_KEY"]
APP_API_KEY = os.environ["APP_API_KEY"]

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ["DB_PORT"])
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]
DB_NAME = os.environ["DB_NAME"]
DB_WORKERS = int(os.environ["DB_WORKERS"])

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])
REDIS_DB = int(os.environ["REDIS_DB"])
REDIS_USER = os.getenv("REDIS_USER") or None
REDIS_PASS = os.environ["REDIS_PASS"]
REDIS_USE_SSL = read_bool(os.environ["REDIS_USE_SSL"])

SCORE_SERVICE_BASE_URL = os.environ["SCORE_SERVICE_BASE_URL"]
PERFORMANCE_SERVICE_BASE_URL = os.environ["PERFORMANCE_SERVICE_BASE_URL"]
BEATMAPS_SERVICE_BASE_URL = os.environ["BEATMAPS_SERVICE_BASE_URL"]

SHUTDOWN_HTTP_CONNECTION_TIMEOUT = int(os.environ["SHUTDOWN_HTTP_CONNECTION_TIMEOUT"])

IRC_PORT = int(os.environ["IRC_PORT"])
IRC_HOSTNAME = os.environ["IRC_HOSTNAME"]
IRC_SERVICE_URL = os.environ["IRC_SERVICE_URL"]
IRC_TOKEN_REQUEST_URL = os.environ["IRC_TOKEN_REQUEST_URL"]

DEBUG = os.environ["DEBUG"] == "1"

AUDIT_LOG_MESSAGE_KEYWORDS = os.environ["AUDIT_LOG_MESSAGE_KEYWORDS"].split(",")

LOCALIZE_ENABLE = os.environ["LOCALIZE_ENABLE"] == "1"

WEBHOOK_NOW_RANKED = os.environ["WEBHOOK_NOW_RANKED"]
WEBHOOK_RANK_REQUESTS = os.environ["WEBHOOK_RANK_REQUESTS"]
WEBHOOK_AC_GENERAL = os.environ["WEBHOOK_AC_GENERAL"]
WEBHOOK_AC_CONFIDENTIAL = os.environ["WEBHOOK_AC_CONFIDENTIAL"]

AMPLITUDE_API_KEY = os.environ["AMPLITUDE_API_KEY"]
AMPLITUDE_DEPLOYMENT_KEY = os.environ["AMPLITUDE_DEPLOYMENT_KEY"]

AMQP_USER = os.environ["AMQP_USER"]
AMQP_PASS = os.environ["AMQP_PASS"]
AMQP_HOST = os.environ["AMQP_HOST"]
AMQP_PORT = int(os.environ["AMQP_PORT"])

BANCHO_LOGIN_ROUTING_KEYS = os.environ["BANCHO_LOGIN_ROUTING_KEYS"].split(",")
