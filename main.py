#!/usr/bin/env python3.9
from __future__ import annotations

import asyncio
import logging.config
import os
import signal
import sys
import traceback
from datetime import datetime
from typing import Optional

import tornado.gen
import tornado.httpserver
import tornado.ioloop
import tornado.web

import lifecycle
import settings
from common.log import logging_config
from handlers import apiChatbotMessageHandler
from handlers import apiIsOnlineHandler
from handlers import apiOnlineUsersHandler
from handlers import apiServerStatusHandler
from handlers import apiVerifiedStatusHandler
from handlers import mainHandler
from objects import channelList
from objects import chatbot
from objects import glob
from objects import streamList


def dump_thread_stacks():
    try:
        os.mkdir("stacktraces")
    except FileExistsError:
        pass
    filename = f"{settings.APP_PORT}-{datetime.now().isoformat()}.txt"
    with open(f"stacktraces/{filename}", "w") as f:
        for thread_id, stack in sys._current_frames().items():
            print(f"Thread ID: {thread_id}", file=f)
            traceback.print_stack(stack, file=f)
            print("\n", file=f)


def signal_handler(signum, frame):
    dump_thread_stacks()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.default_int_handler(signum, frame)


signal.signal(signal.SIGINT, signal_handler)


async def main() -> int:
    http_server: Optional[tornado.httpserver.HTTPServer] = None
    try:
        # TODO: do we need this anymore now with stateless design?
        # (not using filesystem anymore for things like .data/)
        os.chdir(os.path.dirname(os.path.realpath(__file__)))

        await lifecycle.startup()

        await channelList.loadChannels()

        # Initialize stremas
        await streamList.add("main")
        await streamList.add("lobby")

        logging.info(
            "Starting up all services for selected component",
            extra={"component": settings.APP_COMPONENT},
        )
        await chatbot.connect()

        # Start the HTTP server
        API_ENDPOINTS = [
            (r"/", mainHandler.handler),
            (r"/api/v1/isOnline", apiIsOnlineHandler.handler),
            (r"/api/v1/onlineUsers", apiOnlineUsersHandler.handler),
            (r"/api/v1/serverStatus", apiServerStatusHandler.handler),
            (r"/api/v1/verifiedStatus", apiVerifiedStatusHandler.handler),
            # XXX: "fokabot" for legacy reasons
            (r"/api/v1/fokabotMessage", apiChatbotMessageHandler.handler),
        ]
        logging.info("Starting HTTP server")
        glob.application = tornado.web.Application(API_ENDPOINTS)
        http_server = tornado.httpserver.HTTPServer(glob.application)
        http_server.listen(settings.APP_PORT)
        logging.info(
            f"HTTP server listening for clients on port {settings.APP_PORT}",
            extra={
                "port": settings.APP_PORT,
                "endpoints": [e[0] for e in API_ENDPOINTS],
            },
        )
        shutdown_event = asyncio.Event()
        await shutdown_event.wait()
    finally:
        logging.info("Shutting down all services")

        if http_server is not None:
            logging.info("Closing HTTP listener")
            http_server.stop()
            logging.info("Closed HTTP listener")

            logging.info("Closing HTTP connections")
            # Allow grace period for ongoing connections to finish
            await asyncio.wait_for(
                http_server.close_all_connections(),
                timeout=settings.SHUTDOWN_HTTP_CONNECTION_TIMEOUT,
            )
            logging.info("Closed HTTP connections")

        logging.info("Disconnecting from IRC")
        await chatbot.disconnect()
        logging.info("Disconnected from IRC")

        await lifecycle.shutdown()

        logging.info("Goodbye!")

    return 0


if __name__ == "__main__":
    logging_config.configure_logging()
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 0
    exit(exit_code)
