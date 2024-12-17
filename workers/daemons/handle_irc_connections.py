#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import atexit
import datetime
import hashlib
import logging
import os
import signal
import sys
import time
from types import FrameType

sys.path.insert(1, os.path.join(sys.path[0], "../.."))

import lifecycle
import settings
from common import exception_handling
from common.log import logging_config
from common.ripple import user_utils
from constants.irc_commands import IRCCommands
from helpers import irc_helper
from objects import osuToken


class IRCMessage:
    def __init__(self, *, code: IRCCommands, message: str) -> None:
        self.code = code
        self.message = message

    def to_string(self, username: str) -> str:
        return f":{settings.IRC_SERVICE_URL} {self.code.value:03} {username} :{self.message}"


WELCOME_MESSAGE = IRCMessage(
    code=IRCCommands.RPL_WELCOME, message="Welcome to Akatsuki IRC!",
)
MOTD_START_MESSAGE = IRCMessage(code=IRCCommands.RPL_MOTDSTART, message="-")
MOTD_MSG_MESSAGE = IRCMessage(code=IRCCommands.RPL_MOTD, message="- {msg}")
MOTD_END_MESSAGE = IRCMessage(code=IRCCommands.RPL_ENDOFMOTD, message="-")

MISSING_NICK_MESSAGE = IRCMessage(
    code=IRCCommands.ERR_NONICKNAMEGIVEN,
    message="You must provide valid Akatsuki username.",
)

# Auth message is only special case and it requires different IRC codes than usual.
AUTH_WELCOME_MESSAGE = IRCMessage(
    code=IRCCommands.RPL_MOTD, message="Welcome to Akatsuki IRC!",
)
AUTH_START_MESSAGE = IRCMessage(code=IRCCommands.RPL_MOTD, message="-")
AUTH_FAILED_MOTD = [
    "You are required to generate a custom login token to use as a password for this service.",
    "Please use the link below to generate the login token (password):",
    settings.IRC_TOKEN_REQUEST_URL,
]
AUTH_END_MESSAGE = IRCMessage(code=IRCCommands.RPL_MOTD, message="-")
AUTH_FAILED_MESSAGE = IRCMessage(
    code=IRCCommands.ERR_PASSWDMISMATCH, message="Bad authentication token.",
)

AddressType = tuple[str, int]


class IRCClient:
    def __init__(
        self,
        address: AddressType,
        server: IRCServer,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._server = server
        self._reader = reader
        self._writer = writer
        self._address = address

        self.ip = address[0]
        self.is_authenticated = False
        self.should_receive = True

        self.token_id: str | None = None

        # Authentication state variables
        self.__username: str | None = None
        self.__login_token: str | None = None

        # State variables that are updated on every event
        self.irc_username: str = ""
        self.priviliges: int = 0
        self.last_activity = time.perf_counter()

        self.login_time = datetime.datetime.now()
        self.channels: set[str] = set()

    @property
    def irc_host_username(self) -> str:
        return f"{self.irc_username}!{settings.IRC_HOSTNAME}"

    @property
    def irc_prefix(self) -> str:
        return irc_helper.irc_prefix(self.priviliges, is_irc=True)

    @property
    def public_username(self) -> str:
        return self.irc_prefix + self.irc_username

    def __str__(self) -> str:
        return self.irc_host_username

    # Client related stuff

    def _client_update_state(self, token: osuToken.Token) -> None:
        self.irc_username = irc_helper.irc_username_safe(token["username"])
        self.priviliges = token["privileges"]
        self.latest_activity = token["ping_time"]

    # keep relationship with osuToken very close
    async def _client_ensure_token(self) -> None | osuToken.Token:
        assert self.token_id is not None

        token = await osuToken.get_token(self.token_id)
        if token is None:
            logging.warning(
                "Failed to ensure token for IRC client",
                extra={
                    "ip": self.ip,
                    "token_id": self.token_id,
                    "username": self.irc_username,
                },
            )
            await self._client_disconnect()
            return

        token = await osuToken.update_token(
            self.token_id, ping_time=time.perf_counter(),
        )
        if token is None:
            logging.warning(
                "Failed to ensure token for IRC client",
                extra={
                    "ip": self.ip,
                    "token_id": self.token_id,
                    "username": self.irc_username,
                },
            )
            await self._client_disconnect()
            return

        self._client_update_state(token)
        return token

    async def _client_authenticate(self) -> None:
        assert self.__username is not None
        assert self.__login_token is not None

        username_irc = irc_helper.irc_username_safe(self.__username)
        username_safe = user_utils.get_safe_username(self.__username)
        login_token_hash = hashlib.md5(self.__login_token.encode("utf-8")).hexdigest()

        user_resp = await irc_helper.irc_authenticate(username_safe, login_token_hash)
        if user_resp is None:
            logging.warning(
                "Failed to authenticate IRC client",
                extra={"ip": self.ip, "username": self.__username},
            )

            await self.send_auth_failed_message()
            await self._client_disconnect()
            return

        # Logout any existing sessions, both username and IP
        # TODO: possibly overkill?
        existing_client_username = self._server.get_client_by_username(username_irc)
        if existing_client_username is not None:
            await existing_client_username._client_disconnect()

        existing_clients_ip = self._server.get_clients_by_ip(self.ip, self._address[1])
        if existing_clients_ip is not None:
            for client in existing_clients_ip:
                await client._client_disconnect()

        token = await irc_helper.irc_login(user_resp["user_id"], self.ip)
        self.token_id = token["token_id"]

        self.is_authenticated = True
        self._client_update_state(token)

        # Reset login token to prevent it from being used again
        self.__username = None
        self.__login_token = None

        logging.info(
            "IRC client authenticated",
            extra={
                "ip": self.ip,
                "token_id": self.token_id,
                "username": self.irc_username,
            },
        )
        await self.send_welcome_message()

    async def _client_receive(self) -> None:
        while (
            self.is_authenticated
            or (datetime.datetime.now() - self.login_time).seconds < 2
        ) and self.should_receive:
            # If not authenticated, will timeout data to allow while loop check
            # XXX: Check how this affects performance
            if not self.is_authenticated:
                try:
                    data = await asyncio.wait_for(self._reader.read(1024), timeout=1.0)
                except TimeoutError:
                    continue
            else:
                data = await self._reader.read(1024)

            if not data:
                return

            if self.is_authenticated:
                await self._client_ensure_token()

            commands = data.decode("utf-8").strip().split("\n")
            for command in commands:
                if not command:
                    continue

                command, *args = command.split(" ")

                command_handler = getattr(self, f"_handle_{command.lower()}", None)
                if command_handler is not None:
                    await command_handler(args)

            # Username is sent on NICK command and password is sent on PASS command
            # wait until both are received before authenticating.
            if not self.is_authenticated and (
                self.__username is not None and self.__login_token is not None
            ):
                await self._client_authenticate()

        # Broke out of loop, possibly client never authenticated.
        if self.should_receive and not self.is_authenticated:
            logging.warning(
                "IRC client never authenticated",
                extra={"ip": self.ip},
            )
            await self.send_auth_failed_message()
            await self._client_disconnect()

    async def _client_disconnect(self) -> None:
        # TODO: add disconnection message
        self.should_receive = False
        self._writer.close()

        if self.is_authenticated:
            assert self.token_id is not None
            await irc_helper.irc_logout(self.token_id)

        self._server.remove_client(self._address)
        logging.info(
            "IRC client disconnected",
            extra={
                "ip": self.ip,
                "token_id": self.token_id,
                "username": self.irc_username,
            },
        )

    async def send(self, message: str) -> None:
        self._writer.write(f"{message}\r\n".encode())
        await self._writer.drain()

    async def send_auth_failed_message(self) -> None:
        await self.send(AUTH_WELCOME_MESSAGE.to_string(self.irc_username))
        await self.send(AUTH_START_MESSAGE.to_string(self.irc_username))

        for msg in AUTH_FAILED_MOTD:
            await self.send(
                MOTD_MSG_MESSAGE.to_string(self.irc_username).format(msg=msg),
            )

        await self.send(AUTH_END_MESSAGE.to_string(self.irc_username))
        await self.send(AUTH_FAILED_MESSAGE.to_string(self.irc_username))

    async def send_welcome_message(self) -> None:
        await self.send(WELCOME_MESSAGE.to_string(self.irc_username))
        await self.send(MOTD_START_MESSAGE.to_string(self.irc_username))

        for msg in self._server._motd:
            await self.send(
                MOTD_MSG_MESSAGE.to_string(self.irc_username).format(msg=msg),
            )

        await self.send(MOTD_END_MESSAGE.to_string(self.irc_username))

    # IRC command handlers

    async def _handle_nick(self, args: list[str]) -> None:
        if not any(args):
            await self.send(MISSING_NICK_MESSAGE.to_string(self.irc_username))
            await self._client_disconnect()
            return

        username = irc_helper.clean_irc_variable(args[0])
        if not username:
            await self.send(MISSING_NICK_MESSAGE.to_string(self.irc_username))
            await self._client_disconnect()
            return

        if not self.is_authenticated:
            self.__username = username

    async def _handle_pass(self, args: list[str]) -> None:
        if not any(args):
            await self.send_auth_failed_message()
            await self._client_disconnect()
            return

        login_token = irc_helper.clean_irc_variable(args[0])
        if not login_token:
            await self.send_auth_failed_message()
            await self._client_disconnect()
            return

        if not self.is_authenticated:
            self.__login_token = login_token

    async def _handle_ping(self, args: list[str]) -> None:
        ping_args = " ".join(args)
        await self.send(f":{settings.IRC_HOSTNAME} PONG {ping_args}")

    async def _handle_quit(self, _) -> None:
        await self._client_disconnect()


class IRCServer:
    def __init__(
        self,
        address: str,
        port: int,
        *,
        connection_timeout: int = 180,
    ) -> None:
        self._address = address
        self._port = port
        self._connection_timeout = connection_timeout

        self._server: asyncio.Server | None = None
        self._clients: dict[AddressType, IRCClient] = {}  # AddressType -> IRCClient
        self._motd: list[str] = []  # fancy motd on every login

        self._server_start_time = time.perf_counter()
        self._server_should_close = False

    def _ensure_client(
        self,
        address: AddressType,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> IRCClient:
        client = self._clients.get(address)

        if client is None:
            client = IRCClient(
                address=address,
                server=self,
                reader=reader,
                writer=writer,
            )
            self._clients[address] = client
        return client

    async def _send_outstanding_packets(self, client: IRCClient) -> None: ...

    async def _disconnect_inactive_client(
        self, client: IRCClient, current_time: float,
    ) -> None: ...

    def get_client_by_username(self, username: str) -> IRCClient | None:
        for client in self._clients.values():
            if client.irc_username == username:
                return client
        return None

    def get_clients_by_ip(
        self, ip: str, excluding_port: int | None = None,
    ) -> list[IRCClient] | None:
        clients = []
        for client in self._clients.values():
            if client._address[0] == ip and (
                excluding_port is not None and client._address[1] != excluding_port
            ):
                clients.append(client)

        if any(clients):
            return clients

        return None

    def remove_client(self, address: AddressType) -> None:
        self._clients.pop(address, None)

    async def _handle_incoming_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        address = writer.get_extra_info("peername")
        if address is None:
            return

        client = self._ensure_client(
            address,
            reader=reader,
            writer=writer,
        )
        logging.info(
            "New IRC connection accepted",
            extra={"ip": client.ip},
        )

        try:
            await client._client_receive()
        except Exception:
            logging.exception(
                "An error occurred while handling an IRC connection",
                extra={
                    "ip": client.ip,
                    "token_id": client.token_id,
                    "username": client.irc_username,
                },
            )
            await client._client_disconnect()

    async def listen(self) -> None:
        assert SHUTDOWN_EVENT is not None

        self._server = await asyncio.start_server(
            self._handle_incoming_connection,
            self._address,
            self._port,
        )

        while not SHUTDOWN_EVENT.is_set():
            await asyncio.sleep(1)

            await self._server.start_serving()
            current_time = time.perf_counter()

            for client in self._clients.values():
                await self._send_outstanding_packets(client)
                await self._disconnect_inactive_client(client, current_time)

    async def start(self) -> None:
        with open("irc_motd.txt") as motd_file:
            self._motd = motd_file.readlines()

        logging.info(
            "IRC service started!",
            extra={"port": self._port},
        )
        await self.listen()

    async def stop(self) -> None:
        assert self._server is not None

        tasks = [
            asyncio.create_task(client._client_disconnect())
            for client in self._clients.values()
        ]
        if any(tasks):
            await asyncio.wait(tasks)

        self._server.close()
        await self._server.wait_closed()


SHUTDOWN_EVENT: asyncio.Event | None = None


def handle_shutdown_event(signum: int, frame: FrameType | None) -> None:
    logging.info("Received shutdown signal", extra={"signum": signal.strsignal(signum)})
    if SHUTDOWN_EVENT is not None:
        SHUTDOWN_EVENT.set()


signal.signal(signal.SIGTERM, handle_shutdown_event)


async def main() -> int:
    global SHUTDOWN_EVENT
    SHUTDOWN_EVENT = asyncio.Event()
    try:
        server = IRCServer("0.0.0.0", settings.IRC_PORT)
        await lifecycle.startup()

        await server.start()
        await SHUTDOWN_EVENT.wait()
    finally:
        await server.stop()
        await lifecycle.shutdown()

    return 0


if __name__ == "__main__":
    logging_config.configure_logging()
    exception_handling.hook_exception_handlers()
    atexit.register(exception_handling.unhook_exception_handlers)
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 0
    exit(exit_code)
