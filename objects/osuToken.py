from __future__ import annotations

import logging
from time import localtime
from time import strftime
from time import time
from typing import TypedDict
from typing import cast
from uuid import uuid4

import orjson

from common import channel_utils
from common.constants import actions
from common.constants import gameModes
from common.constants import privileges
from common.log import logger
from common.ripple import user_utils
from common.types import UNSET
from common.types import Unset
from constants import CHATBOT_USER_ID
from constants import exceptions
from constants import serverPackets
from helpers import chatHelper as chat
from objects import channelList
from objects import glob
from objects import match
from objects import stream_messages
from objects import streamList

# (set) bancho:tokens
# (json obj) bancho:tokens:{token_id}
# (set) bancho:tokens:{token_id}:streams
# (set) bancho:tokens:{token_id}:channels
# (set[userid]) bancho:tokens:{token_id}:spectators
# (list) bancho:tokens:{token_id}:messages
# (list[userid]) bancho:tokens:{token_id}:sent_away_messages
# (stream) streams:tokens/{token_id}:messages


class LastNp(TypedDict):
    beatmap_id: int
    mods: int
    accuracy: float


class Token(TypedDict):
    token_id: str
    user_id: int
    username: str
    # safe_username: str
    privileges: int
    whitelist: int
    # staff: bool
    # restricted: bool
    kicked: bool
    login_time: float
    ping_time: float
    utc_offset: int
    # streams: list[stream.Stream]
    tournament: bool
    # messages_buffer: list[str]
    block_non_friends_dm: bool
    # spectators: list[osuToken.Token]
    spectating_token_id: str | None
    spectating_user_id: int | None
    # spectator_lock: RLock
    latitude: float
    longitude: float
    # joinedChannels: list[str]
    ip: str
    country: int
    away_message: str | None
    # sent_away_messages: list[int]
    match_id: int | None

    match_slot_id: int | None

    last_np: LastNp | None
    silence_end_time: int
    protocol_version: int
    spam_rate: int

    # stats
    action_id: int
    action_text: str
    action_md5: str
    action_mods: int
    game_mode: int
    relax: bool
    autopilot: bool
    beatmap_id: int
    ranked_score: int
    accuracy: float
    playcount: int
    total_score: int
    global_rank: int
    pp: int

    amplitude_device_id: str | None


def safeUsername(username: str) -> str:
    """
    Return `username`'s safe username
    (all lowercase and underscores instead of spaces)

    :param username: unsafe username
    :return: safe username
    """

    return username.lower().strip().replace(" ", "_")


# CRUD


def make_key(token_id: str) -> str:
    return f"bancho:tokens:{token_id}"


async def create_token(
    *,
    user_id: int,
    username: str,
    privileges: int,
    whitelist: int,
    ip: str,
    utc_offset: int,
    tournament: bool,
    block_non_friends_dm: bool,
    amplitude_device_id: str | None,
) -> Token:
    token_id = str(uuid4())
    creation_time = time()

    token: Token = {
        "token_id": token_id,
        "user_id": user_id,
        "username": username,
        "privileges": privileges,
        "whitelist": whitelist,
        "kicked": False,
        "login_time": creation_time,
        "ping_time": creation_time,
        "utc_offset": utc_offset,
        "tournament": tournament,
        "block_non_friends_dm": block_non_friends_dm,
        "spectating_token_id": None,
        "spectating_user_id": None,
        "latitude": 0.0,
        "longitude": 0.0,
        "ip": ip,
        "country": 0,
        "away_message": None,
        "match_id": None,
        "match_slot_id": None,
        "last_np": None,
        "silence_end_time": 0,
        "protocol_version": 0,
        "spam_rate": 0,
        "action_id": actions.IDLE,
        "action_text": "",
        "action_md5": "",
        "action_mods": 0,
        "game_mode": gameModes.STD,
        "relax": False,
        "autopilot": False,
        "beatmap_id": 0,
        "ranked_score": 0,
        "accuracy": 0.0,
        "playcount": 0,
        "total_score": 0,
        "global_rank": 0,
        "pp": 0,
        "amplitude_device_id": amplitude_device_id,
    }

    safe_name = safeUsername(username)

    async with glob.redis.pipeline() as pipe:
        await pipe.hset("bancho:tokens:json", token_id, orjson.dumps(token))
        await pipe.set(f"bancho:tokens:ids:{user_id}", token_id)
        await pipe.set(f"bancho:tokens:names:{safe_name}", token_id)
        await pipe.hset(
            f"{make_key(token_id)}:stream_offsets",
            f"{make_key(token_id)}:packet_queue",
            "0-0",
        )
        await pipe.execute()

    return token


async def get_token_ids() -> set[str]:
    raw_token_ids: list[bytes] = await glob.redis.hkeys("bancho:tokens:json")
    return {token_id.decode() for token_id in raw_token_ids}


async def get_online_players_count() -> int:
    return await glob.redis.hlen("bancho:tokens:json")


async def get_token(token_id: str) -> Token | None:
    token = await glob.redis.hget("bancho:tokens:json", token_id)
    if token is None:
        return None
    return cast(Token, orjson.loads(token))


async def get_tokens() -> list[Token]:
    # TODO: use an iterative approach for these, provide a generator api
    # to allow callers to reduce memory usage and improve performance.
    # (If callers really want all the data, they can exhaust the iterator)
    raw_tokens = (await glob.redis.hgetall("bancho:tokens:json")).values()
    return cast(list[Token], [orjson.loads(token) for token in raw_tokens])


# TODO: get_limited_tokens with a more basic model


async def get_token_by_user_id(user_id: int) -> Token | None:
    token_id: bytes | None = await glob.redis.get(f"bancho:tokens:ids:{user_id}")
    if token_id is None:
        return None

    token = await get_token(token_id.decode())
    if token is not None:
        return token

    return None


async def get_token_by_username(username: str) -> Token | None:
    token_id: bytes | None = await glob.redis.get(
        f"bancho:tokens:names:{safeUsername(username)}",
    )
    if token_id is None:
        return None

    token = await get_token(token_id.decode())
    if token is not None:
        return token

    return None


async def get_all_tokens_by_user_id(user_id: int) -> list[Token]:
    tokens = await get_tokens()
    return [token for token in tokens if token["user_id"] == user_id]


async def get_all_tokens_by_username(username: str) -> list[Token]:
    tokens = await get_tokens()
    return [token for token in tokens if token["username"] == username]


async def update_token(
    token_id: str,
    *,
    # user_id: Optional[int] = None,
    username: str | None = None,
    privileges: int | None = None,
    whitelist: int | None = None,
    kicked: bool | None = None,
    # login_time: Optional[float] = None,
    ping_time: float | None = None,
    # utc_offset: Optional[int] = None,
    # tournament: Optional[bool] = None,
    block_non_friends_dm: bool | None = None,
    spectating_token_id: str | None | Unset = UNSET,
    spectating_user_id: int | None | Unset = UNSET,
    latitude: float | None = None,
    longitude: float | None = None,
    ip: str | None = None,  # ?
    country: int | None = None,
    away_message: str | None | Unset = UNSET,
    match_id: int | None | Unset = UNSET,
    match_slot_id: int | None | Unset = UNSET,
    last_np: LastNp | None | Unset = UNSET,
    silence_end_time: int | None = None,
    protocol_version: int | None = None,
    spam_rate: int | None = None,
    action_id: int | None = None,
    action_text: str | None = None,
    action_md5: str | None = None,
    action_mods: int | None = None,
    game_mode: int | None = None,
    relax: bool | None = None,
    autopilot: bool | None = None,
    beatmap_id: int | None = None,
    ranked_score: int | None = None,
    accuracy: float | None = None,
    playcount: int | None = None,
    total_score: int | None = None,
    global_rank: int | None = None,
    pp: int | None = None,
    amplitude_device_id: str | None = None,
) -> Token | None:
    token = await get_token(token_id)
    if token is None:
        return None

    if username is not None:
        token["username"] = username
    if privileges is not None:
        token["privileges"] = privileges
    if whitelist is not None:
        token["whitelist"] = whitelist
    if kicked is not None:
        token["kicked"] = kicked
    if ping_time is not None:
        token["ping_time"] = ping_time
    if block_non_friends_dm is not None:
        token["block_non_friends_dm"] = block_non_friends_dm
    if not isinstance(spectating_token_id, Unset):
        token["spectating_token_id"] = spectating_token_id
    if not isinstance(spectating_user_id, Unset):
        token["spectating_user_id"] = spectating_user_id
    if latitude is not None:
        token["latitude"] = latitude
    if longitude is not None:
        token["longitude"] = longitude
    if ip is not None:
        token["ip"] = ip
    if country is not None:
        token["country"] = country
    if not isinstance(away_message, Unset):
        token["away_message"] = away_message
    if not isinstance(match_id, Unset):
        token["match_id"] = match_id
    if not isinstance(match_slot_id, Unset):
        token["match_slot_id"] = match_slot_id
    if not isinstance(last_np, Unset):
        token["last_np"] = last_np
    if silence_end_time is not None:
        token["silence_end_time"] = silence_end_time
    if protocol_version is not None:
        token["protocol_version"] = protocol_version
    if spam_rate is not None:
        token["spam_rate"] = spam_rate
    if action_id is not None:
        token["action_id"] = action_id
    if action_text is not None:
        token["action_text"] = action_text
    if action_md5 is not None:
        token["action_md5"] = action_md5
    if action_mods is not None:
        token["action_mods"] = action_mods
    if game_mode is not None:
        token["game_mode"] = game_mode
    if relax is not None:
        token["relax"] = relax
    if autopilot is not None:
        token["autopilot"] = autopilot
    if beatmap_id is not None:
        token["beatmap_id"] = beatmap_id
    if ranked_score is not None:
        token["ranked_score"] = ranked_score
    if accuracy is not None:
        token["accuracy"] = accuracy
    if playcount is not None:
        token["playcount"] = playcount
    if total_score is not None:
        token["total_score"] = total_score
    if global_rank is not None:
        token["global_rank"] = global_rank
    if pp is not None:
        token["pp"] = pp
    if amplitude_device_id is not None:
        token["amplitude_device_id"] = amplitude_device_id

    async with glob.redis.pipeline() as pipe:
        await pipe.hset("bancho:tokens:json", token_id, orjson.dumps(token))
        await pipe.execute()

    return token


async def delete_token(token_id: str) -> None:
    token = await get_token(token_id)
    if token is None:
        return

    async with glob.redis.pipeline() as pipe:
        await pipe.delete(f"bancho:tokens:ids:{token['user_id']}")
        await pipe.delete(f"bancho:tokens:names:{safeUsername(token['username'])}")
        await pipe.hdel("bancho:tokens:json", token_id)
        await pipe.delete(f"{make_key(token_id)}:channels")
        await pipe.delete(f"{make_key(token_id)}:spectators")
        await pipe.delete(f"{make_key(token_id)}:streams")
        await pipe.delete(f"{make_key(token_id)}:message_history")
        await pipe.delete(f"{make_key(token_id)}:sent_away_messages")
        await pipe.delete(f"{make_key(token_id)}:processing_lock")
        await pipe.execute()


# joined channels


async def get_joined_channels(token_id: str) -> set[str]:
    """Returns a set of channel names"""
    raw_channels: set[bytes] = await glob.redis.smembers(
        f"{make_key(token_id)}:channels",
    )
    return {x.decode() for x in raw_channels}


# spectators


async def get_spectators(token_id: str) -> set[int]:
    raw_spectators: set[bytes] = await glob.redis.smembers(
        f"{make_key(token_id)}:spectators",
    )
    return {int(raw_spectator) for raw_spectator in raw_spectators}


async def remove_spectator(token_id: str, spectator_user_id: int) -> None:
    await glob.redis.srem(f"{make_key(token_id)}:spectators", spectator_user_id)


async def add_spectator(token_id: str, spectator_user_id: int) -> None:
    await glob.redis.sadd(f"{make_key(token_id)}:spectators", spectator_user_id)


# streams


async def get_streams(token_id: str) -> set[str]:
    raw_streams: set[bytes] = await glob.redis.smembers(f"{make_key(token_id)}:streams")
    return {raw_stream.decode() for raw_stream in raw_streams}


async def add_stream(token_id: str, stream_name: str) -> None:
    await glob.redis.sadd(f"{make_key(token_id)}:streams", stream_name)

    stream_offset = await stream_messages.get_latest_message_id(stream_name)
    await glob.redis.hset(
        f"{make_key(token_id)}:stream_offsets",
        stream_messages.make_key(stream_name),
        stream_offset,
    )


async def remove_stream(token_id: str, stream_name: str) -> None:
    await glob.redis.srem(f"{make_key(token_id)}:streams", stream_name)
    await glob.redis.hdel(
        f"{make_key(token_id)}:stream_offsets",
        stream_messages.make_key(stream_name),
    )


# messages
# (list) bancho:tokens:{token_id}:message_history


async def get_message_history(token_id: str) -> list[str]:
    raw_history: list[bytes] = await glob.redis.lrange(
        f"{make_key(token_id)}:message_history",
        0,
        -1,
    )
    return [raw_message.decode() for raw_message in raw_history]


async def add_message_to_history(token_id: str, message: str) -> None:
    await glob.redis.rpush(f"{make_key(token_id)}:message_history", message)


# away messages
# (set[userid]) bancho:tokens:{token_id}:sent_away_messages


async def get_users_whove_seen_away_message(token_id: str) -> set[int]:
    raw_messages: set[bytes] = await glob.redis.smembers(
        f"{make_key(token_id)}:sent_away_messages",
    )
    return {int(raw_message.decode()) for raw_message in raw_messages}


async def add_user_whos_seen_away_message(token_id: str, user_id: int) -> None:
    await glob.redis.sadd(f"{make_key(token_id)}:sent_away_messages", user_id)


# properties


def is_staff(token_privileges: int) -> bool:
    return token_privileges & privileges.ADMIN_CHAT_MOD != 0


def is_restricted(token_privileges: int) -> bool:
    return token_privileges & privileges.USER_PUBLIC == 0


#####


async def enqueue(token_id: str, data: bytes) -> None:
    """
    Add bytes (packets) to queue

    :param data: (packet) bytes to enqueue
    """
    token = await get_token(token_id)
    if token is None:
        return

    # Never enqueue data to the chatbot
    if token["user_id"] == CHATBOT_USER_ID:
        return

    if len(data) >= 10 * 10**6:
        logger.warning(
            "Enqueuing very large amount of data to token",
            extra={"num_bytes": len(data), "token_id": token_id},
        )

    await stream_messages.broadcast_data(f"tokens/{token_id}:messages", data)


async def joinChannel(token_id: str, channel_name: str) -> None:
    """
    Join a channel

    :param channelObject: channel object
    :raises: exceptions.userAlreadyInChannelException()
                exceptions.channelNoPermissionsException()
    """
    token = await get_token(token_id)
    if token is None:
        return

    current_channels = await get_joined_channels(token_id)

    if channel_name in current_channels:
        raise exceptions.userAlreadyInChannelException()

    channel = await channelList.getChannel(channel_name)
    if channel is None:
        raise exceptions.channelUnknownException()

    # Make sure we have read permissions.

    # premium requires premium
    if (
        channel_name == "#premium"
        and token["privileges"] & privileges.USER_PREMIUM == 0
    ):
        raise exceptions.channelNoPermissionsException()

    # supporter requires supporter
    if (
        channel_name == "#supporter"
        and token["privileges"] & privileges.USER_DONOR == 0
    ):
        raise exceptions.channelNoPermissionsException()

    # non-public channels require staff or bot
    if (not channel["public_read"]) and not (
        is_staff(token["privileges"]) or token["user_id"] == CHATBOT_USER_ID
    ):
        raise exceptions.channelNoPermissionsException()

    await glob.redis.sadd(f"{make_key(token_id)}:channels", channel_name)
    await joinStream(token_id, f"chat/{channel_name}")

    client_name = channel_utils.get_client_name(channel_name)
    await enqueue(token_id, serverPackets.channelJoinSuccess(client_name))


async def partChannel(token_id: str, channel_name: str) -> None:
    """
    Remove channel from joined channels list

    :param channel_name: channel name
    """
    joined_channels = await get_joined_channels(token_id)
    if channel_name not in joined_channels:
        raise exceptions.userNotInChannelException()

    await glob.redis.srem(f"{make_key(token_id)}:channels", channel_name)
    await leaveStream(token_id, f"chat/{channel_name}")


async def setLocation(token_id: str, latitude: float, longitude: float) -> None:
    """
    Set client location

    :param latitude: latitude
    :param longitude: longitude
    """
    token = await get_token(token_id)
    if token is None:
        return

    await update_token(
        token_id,
        latitude=latitude,
        longitude=longitude,
    )


async def startSpectating(token_id: str, host_token_id: str) -> None:
    """
    Set the spectating user to userID, join spectator stream and chat channel
    and send required packets to host

    :param host: host osuToken object
    """
    token = await get_token(token_id)
    if token is None:
        return

    host_token = await get_token(host_token_id)
    if host_token is None:
        return

    # Stop spectating old client, if any
    if token["spectating_token_id"] is not None:
        await stopSpectating(token_id)

    # Set new spectator host
    await update_token(
        token_id,
        spectating_token_id=host_token_id,
        spectating_user_id=host_token["user_id"],
    )

    # Add us to host's spectator list
    await add_spectator(host_token_id, token["user_id"])

    # Create and join spectator stream
    streamName = f"spect/{host_token['user_id']}"
    await streamList.add(streamName)
    await joinStream(token_id, streamName)
    await joinStream(host_token_id, streamName)

    # Send spectator join packet to host
    await enqueue(host_token_id, serverPackets.addSpectator(token["user_id"]))

    # Create and join #spectator (#spect_userid) channel
    await channelList.addChannel(
        name=f"#spect_{host_token['user_id']}",
        description=f"Spectator lobby for host {host_token['username']}",
        public_read=True,
        public_write=False,
        instance=True,
    )
    await chat.join_channel(
        token_id=token_id,
        channel_name=f"#spect_{host_token['user_id']}",
        allow_instance_channels=True,
    )

    spectators = await get_spectators(host_token["token_id"])
    if len(spectators) == 1:
        # First spectator, send #spectator join to host too
        await chat.join_channel(
            token_id=host_token_id,
            channel_name=f"#spect_{host_token['user_id']}",
            allow_instance_channels=True,
        )

    # Send fellow spectator join to all clients
    await stream_messages.broadcast_data(
        streamName,
        serverPackets.fellowSpectatorJoined(token["user_id"]),
    )

    # Get current spectators list
    spectators = await get_spectators(host_token["token_id"])
    for spectator_user_id in spectators:
        if spectator_user_id != token["user_id"]:
            await enqueue(
                token_id,
                serverPackets.fellowSpectatorJoined(token["user_id"]),
            )


async def stopSpectating(token_id: str) -> None:
    """
    Stop spectating, leave spectator stream and channel
    and send required packets to host

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    # Remove our token id from host's spectators
    if token["spectating_token_id"] is None:
        logging.warning(
            "Could not stop spectating, token is not spectating anyone.",
            extra={"token_id": token_id},
        )
        return

    host_token = await get_token(token["spectating_token_id"])
    stream_name = f"spect/{token['spectating_user_id']}"

    # Remove us from host's spectators list,
    # leave spectator stream
    # and end the spectator left packet to host
    await leaveStream(token_id, stream_name)

    if host_token:
        await remove_spectator(host_token["token_id"], token["user_id"])
        await enqueue(
            host_token["token_id"],
            serverPackets.removeSpectator(token["user_id"]),
        )

        fellow_left_packet = serverPackets.fellowSpectatorLeft(token["user_id"])
        # and to all other spectators
        spectators = await get_spectators(host_token["token_id"])
        for spectator in spectators:
            spectator_token = await get_token_by_user_id(spectator)
            if spectator_token is None:
                continue

            await enqueue(spectator_token["token_id"], fellow_left_packet)

        # If nobody is spectating the host anymore, close #spectator channel
        # and remove host from spect stream too
        if not spectators:
            await chat.part_channel(
                token_id=host_token["token_id"],
                channel_name=f"#spect_{host_token['user_id']}",
                notify_user_of_kick=True,
                allow_instance_channels=True,
            )
            await leaveStream(host_token["token_id"], stream_name)

        # Console output
        # log.info("{} is no longer spectating {}. Current spectators: {}.".format(self.username, self.spectatingUserID, hostToken.spectators))

    # Part #spectator channel
    await chat.part_channel(
        token_id=token_id,
        channel_name=f"#spect_{token['spectating_user_id']}",
        notify_user_of_kick=True,
        allow_instance_channels=True,
    )

    # Set our spectating user to None
    await update_token(
        token_id,
        spectating_token_id=None,
        spectating_user_id=None,
    )


async def updatePingTime(token_id: str) -> None:
    """
    Update latest ping time to current time

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    await update_token(
        token_id,
        ping_time=time(),
    )


async def joinMatch(token_id: str, match_id: int) -> bool:
    """
    Set match to match_id, join match stream and channel

    :param match_id: new match ID
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return False

    # Make sure the match exists
    multiplayer_match = await match.get_match(match_id)
    if multiplayer_match is None:
        return False

    # Stop spectating
    await stopSpectating(token_id)

    # Leave other matches
    if token["match_id"] is not None and token["match_id"] != match_id:
        await leaveMatch(token_id)

    # Try to join match
    match_slot_id = await match.userJoin(multiplayer_match["match_id"], token_id)
    if match_slot_id is None:
        await enqueue(token_id, serverPackets.matchJoinFail)
        return False

    # Set matchID, join stream, channel and send packet
    await update_token(
        token_id,
        match_id=match_id,
        match_slot_id=match_slot_id,
    )
    await joinStream(token_id, match.create_stream_name(multiplayer_match["match_id"]))
    await chat.join_channel(
        token_id=token_id,
        channel_name=f"#mp_{match_id}",
        allow_instance_channels=True,
    )
    await enqueue(token_id, await serverPackets.matchJoinSuccess(match_id))

    if multiplayer_match["is_tourney"]:
        # Alert the user if we have just joined a tourney match
        await enqueue(
            token_id,
            serverPackets.notification("You are now in a tournament match."),
        )
        # If an user joins, then the ready status of the match changes and
        # maybe not all users are ready.
        await match.sendReadyStatus(multiplayer_match["match_id"])

    bot_token = await get_token_by_user_id(CHATBOT_USER_ID)
    assert bot_token is not None

    mp_message = match.get_match_history_message(
        multiplayer_match["match_id"],
        multiplayer_match["match_history_private"],
    )

    await enqueue(
        token_id,
        serverPackets.sendMessage(
            fro=bot_token["username"],
            to="#multiplayer",
            message=mp_message,
            fro_id=bot_token["user_id"],
        ),
    )

    return True


async def leaveMatch(token_id: str) -> None:
    """
    Leave joined match, match stream and match channel

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    # Make sure we are in a match
    if token["match_id"] is None:
        return

    # Part #multiplayer channel and streams (/ and /playing)
    await chat.part_channel(
        token_id=token_id,
        channel_name=f"#mp_{token['match_id']}",
        notify_user_of_kick=True,
        allow_instance_channels=True,
    )
    await leaveStream(token_id, match.create_stream_name(token["match_id"]))
    await leaveStream(
        token_id,
        match.create_playing_stream_name(token["match_id"]),
    )  # optional

    # Set usertoken match to -1
    leaving_match_id = token["match_id"]
    await update_token(
        token_id,
        match_id=None,
        match_slot_id=None,
    )

    # Make sure the match exists
    multiplayer_match = await match.get_match(leaving_match_id)
    if multiplayer_match is None:
        return

    # Set slot to free
    await match.userLeft(multiplayer_match["match_id"], token_id)

    if multiplayer_match["is_tourney"]:
        # If an user leaves, then the ready status of the match changes and
        # maybe all users are ready. Or maybe nobody is in the match anymore
        await match.sendReadyStatus(multiplayer_match["match_id"])


async def kick(
    token_id: str,
    message: str = "You were kicked from the server.",
    reason: str = "kick",
) -> None:
    """
    Kick this user from the server

    :param message: Notification message to send to this user.
                    Default: "You were kicked from the server."
    :param reason: Kick reason, used in logs. Default: "kick"
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    # Send packet to target
    if message:
        await enqueue(token_id, serverPackets.notification(message))

    await enqueue(token_id, serverPackets.loginFailed)

    # Logout event
    from events import logoutEvent  # TODO: fix circular import

    await logoutEvent.handle(token)

    logger.info(
        "Invalidated a user's bancho session",
        extra={
            "username": token["username"],
            "user_id": token["user_id"],
            "reason": reason,
        },
    )


async def silence(
    token_id: str,
    seconds: int | None = None,
    reason: str = "",
    author: int = CHATBOT_USER_ID,
) -> None:
    """
    Silences this user (db, packet and token)

    :param seconds: silence length in seconds. If None, get it from db. Default: None
    :param reason: silence reason. Default: empty string
    :param author: userID of who has silenced the user. Default: CHATBOT_USER_ID (Aika)
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    if seconds is None:
        # Get silence expire from db if needed
        seconds = await user_utils.get_remaining_silence_time(token["user_id"])
    else:
        # Silence in db and token
        await user_utils.silence(token["user_id"], seconds, reason, author)

    # Silence token
    await update_token(
        token_id,
        silence_end_time=int(time()) + seconds,
    )

    # Send silence packet to user
    await enqueue(token_id, serverPackets.silenceEndTime(seconds))

    # Send silenced packet to everyone else
    await stream_messages.broadcast_data(
        "main",
        serverPackets.userSilenced(token["user_id"]),
    )


async def chat_spam_protection(
    token_id: str,
    *,
    increase_spam_rate: bool = True,
) -> None:
    """
    Silences the user if they are spamming chat messages.

    :param increaseSpamRate: set to True if the user has sent a new message. Default: True
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    spam_rate = token["spam_rate"]

    # Increase the spam rate if needed
    if increase_spam_rate:
        spam_rate += 1
        await update_token(token_id, spam_rate=spam_rate)

    ACCEPTABLE_SPAM_RATE = 10

    # Silence the user if they exceed the acceptable rate
    if spam_rate > ACCEPTABLE_SPAM_RATE:
        await silence(token_id, 5 * 60, "Spamming (auto spam protection)")


async def isSilenced(token_id: str) -> bool:
    """
    Returns True if this user is silenced, otherwise False

    :return: True if this user is silenced, otherwise False
    """
    token = await get_token(token_id)
    if token is None:
        return False

    return token["silence_end_time"] - time() > 0


async def getSilenceSecondsLeft(token_id: str) -> int:
    """
    Returns the seconds left for this user's silence
    (0 if user is not silenced)

    :return: silence seconds left (or 0)
    """
    token = await get_token(token_id)
    if token is None:
        return 0

    return max(0, token["silence_end_time"] - int(time()))


async def updateCachedStats(token_id: str) -> None:
    """
    Update all cached stats for this token

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        logger.warning(
            "Token not found when updating cached stats",
            extra={"token_id": token_id},
        )
        return

    if token["relax"]:
        relax_int = 1
    elif token["autopilot"]:
        relax_int = 2
    else:
        relax_int = 0

    stats = await user_utils.get_user_stats(
        token["user_id"],
        token["game_mode"],
        relax_int,
    )
    if stats is None:
        logger.warning("Stats query returned None")
        return

    await update_token(
        token_id,
        ranked_score=stats["ranked_score"],
        accuracy=stats["avg_accuracy"] / 100,
        playcount=stats["playcount"],
        total_score=stats["total_score"],
        global_rank=stats["global_rank"],
        pp=stats["pp"],
    )


async def notifyUserOfRestrictionStatusChange(token_id: str) -> None:
    """
    Check if this token is restricted. If so, send Aika message

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    old_restricted = is_restricted(token["privileges"])
    restricted = await user_utils.is_restricted(token["user_id"])
    if restricted:
        # Is restricted; notify them of restriction
        await notifyUserOfRestriction(token_id)
    elif old_restricted:
        # Was previously restricted; notify them of unrestriction
        await informUserOfUnrestriction(token_id)


async def disconnectUserIfBanned(token_id: str) -> None:
    """
    Check if this user is banned. If so, disconnect it.

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    if await user_utils.is_banned(token["user_id"]):
        await enqueue(token_id, serverPackets.loginBanned)
        from events import logoutEvent  # TODO: fix circular import

        await logoutEvent.handle(token, deleteToken=False)


async def notifyUserOfRestriction(token_id: str) -> None:
    """
    Send Aika message to user to inform them of the restriciton.

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    aika_token = await get_token_by_user_id(CHATBOT_USER_ID)
    assert aika_token is not None
    await chat.send_message(
        sender_token_id=aika_token["token_id"],
        recipient_name=token["username"],
        message="Your account is currently in restricted mode. Please visit Akatsuki's website for more information.",
    )


async def informUserOfUnrestriction(token_id: str) -> None:
    """
    Send Aika message to alert the user that he has been unrestricted
    and he has to log in again.

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    aika_token = await get_token_by_user_id(CHATBOT_USER_ID)
    assert aika_token is not None
    await chat.send_message(
        sender_token_id=aika_token["token_id"],
        recipient_name=token["username"],
        message="Your account has been unrestricted! Please log in again.",
    )


async def joinStream(token_id: str, name: str) -> None:
    """
    Join a packet stream, or create it if the stream doesn't exist.

    :param name: stream name
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    await streamList.join(name, token_id=token_id)
    if name not in await get_streams(token_id):
        await add_stream(token_id, name)


async def leaveStream(token_id: str, name: str) -> None:
    """
    Leave a packets stream

    "Leaving" is a higher level abstraction than "removing" a stream,
    because it additionally handles the two-way relationship between
    the stream and the user's token.

    :param name: stream name
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    await streamList.leave(name, token_id)
    if name in await get_streams(token_id):
        await remove_stream(token_id, name)


async def leaveAllStreams(token_id: str) -> None:
    """
    Leave all joined packet streams

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    for stream in await get_streams(token_id):
        await leaveStream(token_id, stream)


async def leaveAllChannels(token_id: str) -> None:
    """
    Leave all joined channels

    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    for channel_name in await get_joined_channels(token_id):
        await chat.part_channel(token_id=token_id, channel_name=channel_name)


async def awayCheck(token_id: str, user_id: int) -> bool:
    """
    Returns True if user_id doesn't know that we are away
    Returns False if we are not away or if user_id already knows we are away

    :param user_id: original sender user_id
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return False

    if not token["away_message"]:
        return False

    if user_id in await get_users_whove_seen_away_message(token_id):
        return False

    await add_user_whos_seen_away_message(token_id, user_id)
    return True


async def addMessageInBuffer(token_id: str, channel: str, message: str) -> None:
    """
    Add a message in messages buffer (100 messages, truncated at 1000 chars).
    Used as proof when the user gets reported.

    :param channel: channel
    :param message: message content
    :return:
    """
    token = await get_token(token_id)
    if token is None:
        return

    message_history = await get_message_history(token_id)
    if len(message_history) > 100:
        await glob.redis.lpop(f"{make_key(token_id)}:message_history")
    await add_message_to_history(
        token_id,
        f"{strftime('%H:%M', localtime())} - {token['username']}@{channel}: {message[:1000]}",
    )


async def getMessagesBufferString(token_id: str) -> str:
    """
    Get the content of the messages buffer as a string

    :return: messages buffer content as a string
    """
    return "\n".join(x for x in await get_message_history(token_id))
