from __future__ import annotations

import time
from typing import TypedDict

import orjson
from amplitude.event import BaseEvent

from constants import serverPackets
from objects import glob
from objects import osuToken
from objects import stream_messages
from objects import tokenList


def irc_username_safe(s: str) -> str:
    return s.replace(" ", "_")


def clean_irc_variable(variable: str) -> str:
    return variable.strip(":").strip()


def irc_prefix(privileges: int, is_irc: bool = False) -> str:
    if osuToken.is_staff(privileges):
        return "@"

    if is_irc:
        return "+"

    return ""  # osu clients have no prefix.


class IRCAuthResponse(TypedDict):
    username_safe: str
    user_id: int


async def irc_authenticate(
    username_safe: str, login_token_hash: str,
) -> None | IRCAuthResponse:
    user_data = await glob.db.fetch(
        "SELECT u.username_safe, u.id FROM users u LEFT JOIN irc_tokens t "
        "ON u.id = t.userid WHERE t.token = %s AND u.username_safe = %s",
        [login_token_hash, username_safe],
    )
    if user_data is None:
        return None

    return IRCAuthResponse(
        username_safe=user_data["username_safe"],
        user_id=user_data["id"],
    )


# TODO: rewrite that once dual session handling is implemented.
async def irc_login(user_id: int, ip: str) -> osuToken.Token:
    # TODO: implement dual session handling for IRC and osu client.
    token = await osuToken.get_token_by_user_id(user_id)
    if token is not None:
        await osuToken.kick(
            token_id=token["token_id"], reason="Logged in from another client.",
        )

    token = await tokenList.addToken(
        user_id=user_id,
        ip=ip,
    )

    if not osuToken.is_restricted(token["privileges"]):
        await stream_messages.broadcast_data(
            "main", await serverPackets.userPanel(user_id),
        )

    if glob.amplitude is not None:
        glob.amplitude.track(
            BaseEvent(
                event_type="irc_login",
                user_id=str(user_id),
                device_id=token["amplitude_device_id"],
                event_properties={
                    "username": token["username"],
                    "privileges": token["privileges"],
                    "login_time": token["login_time"],
                    "source": "bancho-service-irc",
                },
                ip=ip,
            ),
        )

    return token


# TODO: rewrite that once dual session handling is implemented.
async def irc_logout(token_id: str) -> None:
    token = await osuToken.get_token(token_id)
    if token is None:
        return

    # Part all joined channels
    await osuToken.leaveAllChannels(token["token_id"])

    # Leave all joined streams
    await osuToken.leaveAllStreams(token["token_id"])

    # Enqueue our disconnection to everyone else, on osu side
    await stream_messages.broadcast_data(
        "main",
        serverPackets.userLogout(token["user_id"]),
    )

    # TODO: implement dual session handling for IRC and osu client.
    await tokenList.deleteToken(token["token_id"])

    # Change username if needed
    newUsername = await glob.redis.get(
        f"ripple:change_username_pending:{token['user_id']}",
    )
    if newUsername:
        assert isinstance(newUsername, bytes)
        await glob.redis.publish(
            "peppy:change_username",
            orjson.dumps(
                {
                    "userID": token["user_id"],
                    "newUsername": newUsername.decode("utf-8"),
                },
            ),
        )

    if glob.amplitude is not None:
        glob.amplitude.track(
            BaseEvent(
                event_type="irc_logout",
                user_id=str(token["user_id"]),
                device_id=token["amplitude_device_id"],
                event_properties={
                    "username": token["username"],
                    "session_duration": time.time() - token["login_time"],
                    "login_time": token["login_time"],
                    "source": "bancho-service-irc",
                },
                ip=token["ip"],
            ),
        )
