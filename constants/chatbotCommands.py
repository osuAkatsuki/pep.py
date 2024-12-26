from __future__ import annotations

import asyncio
import random
import secrets
import time
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Literal
from typing import Optional
from typing import TypedDict
from typing import overload

from amplitude import BaseEvent

import settings
from adapters import beatmaps_service
from common import generalUtils
from common import job_scheduling
from common import performance_utils
from common import profiling
from common import speedrunning
from common.constants import badges
from common.constants import gameModes
from common.constants import mods
from common.constants import privileges
from common.log import audit_logs
from common.log import logger
from common.ripple import scoreUtils
from common.ripple import user_utils
from common.web import discord
from constants import CHATBOT_USER_ID
from constants import CHATBOT_USER_NAME
from constants import exceptions
from constants import matchModModes
from constants import matchScoringTypes
from constants import matchTeams
from constants import matchTeamTypes
from constants import serverPackets
from constants import slotStatuses
from helpers import chatHelper as chat
from objects import channelList
from objects import chatbot
from objects import glob
from objects import match
from objects import matchList
from objects import osuToken
from objects import slot
from objects import stream_messages
from objects import tokenList
from objects.redisLock import redisLock

"""
Commands callbacks

Must have (fro: str, chan: str, msg: list[str]) as args.
:param fro: username of who triggered the command
:param chan: channel"(or username, if PM) where the message was sent
:param message: list containing arguments passed from the message
                [0] = first argument
                [1] = second argument
                . . .

return the message or **False** if there's no response by the bot
TODO: Change False to None, because False doesn't make any sense
"""

DEFAULT_PERFORMANCE_ACCURACY_VALUES = (
    100.0,
    99.0,
    98.0,
    95.0,
)

CommandCallable = Callable[[str, str, list[str]], Awaitable[Optional[str]]]


class Command(TypedDict):
    trigger: str
    privileges: int
    syntax: str | None
    hidden: bool
    callback: CommandCallable


commands: list[Command] = []


# fro: str, chan: str, message: list[str]) -> str:


def command(
    trigger: str,
    privs: int = privileges.USER_NORMAL,
    syntax: str | None = None,
    hidden: bool = False,
) -> Callable[[CommandCallable], CommandCallable]:
    def wrapper(f: CommandCallable) -> CommandCallable:
        commands.append(
            {
                "trigger": trigger,
                "privileges": privs,
                "syntax": syntax,
                "hidden": hidden,
                "callback": f,
            },
        )
        return f

    return wrapper


def get_beatmap_url(beatmap_id: int) -> str:
    return f"https://osu.ppy.sh/beatmaps/{beatmap_id}"


def get_url_embed(url: str, text: str) -> str:
    return f"[{url} {text}]"


# !speedrun start <10m/1h/1d/1w> <mode> <score_type>


@command(trigger="!speedrun start", hidden=False)
async def start_speedrun(fro: str, chan: str, message: list[str]) -> str:
    user_id = await user_utils.get_id_from_username(fro)
    # if user_id not in {1001, 4528}:
    #     return "u may not taste my delicious lettuce"

    try:
        _, timeframe_str, mode_str, score_type_str = message
        timeframe = speedrunning.SpeedrunTimeframe(timeframe_str)
        score_type = speedrunning.ScoreType(score_type_str)
        game_mode = int(mode_str)
    except ValueError:
        return (
            "Invalid syntax. Usage: !speedrun start <10m/1h/1d/1w> <mode> <score_type>"
        )

    active_speedrun = await speedrunning.get_active_user_speedrun(user_id)
    if active_speedrun:
        return "Speedrun already in progress."

    # TODO: cronjob to auto clean these up
    user_speedrun = await speedrunning.create_user_speedrun(
        user_id=user_id,
        game_mode=game_mode,
        timeframe=timeframe,
        score_type=score_type,
    )

    return f"Speedrun started! ID: {user_speedrun.id}"


@command(trigger="!speedrun end", hidden=False)
async def end_speedrun(fro: str, chan: str, message: list[str]) -> str:
    user_id = await user_utils.get_id_from_username(fro)

    speedrun_results = await speedrunning.end_active_user_speedrun(user_id)
    if speedrun_results is None:
        return "No speedrun in progress."

    speedrun = speedrun_results.speedrun
    scores = speedrun_results.scores

    ret = f"Speedrun ended! Total score: {speedrun.score_value:,.2f} in {speedrun.timeframe}\n"
    for score in scores:
        beatmap_url = get_beatmap_url(score.beatmap_id)
        mods_str = f" +{scoreUtils.readableMods(score.mods)}" if score.mods else ""
        ret += f"{get_url_embed(beatmap_url, score.song_name)}: {score.value:,.2f}{mods_str}\n"

    return ret[:-1]


@command("!speedrun best", hidden=False)
async def best_speedrun(fro: str, chan: str, message: list[str]) -> str:
    user_id = await user_utils.get_id_from_username(fro)

    try:
        _, timeframe_str, mode_str, score_type_str = message
        timeframe = speedrunning.SpeedrunTimeframe(timeframe_str)
        game_mode = int(mode_str)
        score_type = speedrunning.ScoreType(score_type_str)
    except ValueError:
        return (
            "Invalid syntax. Usage: !speedrun best <10m/1h/1d/1w> <mode> <score_type>"
        )

    user_speedruns = await speedrunning.get_user_speedruns(
        user_id=user_id,
        game_mode=game_mode,
        score_type=score_type,
        timeframe=timeframe,
    )
    if not user_speedruns:
        return "You must first run"

    best_speedrun = max(user_speedruns, key=lambda s: s.score_value)
    # TODO: show scores under this like !speedrun end
    return f"Your best speedrun: {best_speedrun.score_value:,.2f} in {best_speedrun.timeframe}"


@command(trigger="!help", hidden=True)
async def _help(fro: str, chan: str, message: list[str]) -> str:
    """Show all documented commands the player can access."""
    l = ["Individual commands", "-----------"]

    userID = await user_utils.get_id_from_username(fro)
    user_privs = await user_utils.get_privileges(userID)

    for cmd in commands:
        cmd_trigger = cmd["trigger"]
        cmd_priv = cmd["privileges"]
        cmd_doc = cmd["callback"].__doc__

        if cmd_doc and user_privs & cmd_priv == cmd_priv:
            # doc available & sufficient privileges
            l.append(f"{cmd_trigger}: {cmd_doc}")

    return "\n".join(l)


@command(
    trigger="!addbn",
    syntax="<name>",
    privs=privileges.ADMIN_MANAGE_NOMINATORS,
)
async def addbn(fro: str, chan: str, message: list[str]) -> str:
    """Add BN privileges to a user"""
    if not message:
        return "Invalid command syntax"

    username = message[0]
    if not (targetID := await user_utils.get_id_from_username(username)):
        return "Could not find user"

    current_privileges = await user_utils.get_privileges(targetID)
    new_privileges = (
        current_privileges
        | privileges.ADMIN_MANAGE_BEATMAPS
        | privileges.USER_DONOR
        | privileges.USER_PREMIUM
    )
    await user_utils.set_privileges(targetID, new_privileges)
    target_tokens = await osuToken.get_all_tokens_by_user_id(targetID)
    for token in target_tokens:
        await osuToken.update_token(token["token_id"], privileges=new_privileges)
    await user_utils.add_user_badge(targetID, badges.BEATMAP_NOMINATION)
    await user_utils.set_absolute_donor_expiry_time(targetID, 2147483647)
    return f"{fro} has given BN to {username}."


@command(
    trigger="!removebn",
    syntax="<name>",
    privs=privileges.ADMIN_MANAGE_NOMINATORS,
)
async def removebn(fro: str, chan: str, message: list[str]) -> str:
    """Remove BN privileges from a user"""
    if not message:
        return "Invalid command syntax"

    username = message[0]
    if not (targetID := await user_utils.get_id_from_username(username)):
        return "Could not find user"

    current_privileges = await user_utils.get_privileges(targetID)
    new_privileges = (
        current_privileges
        & ~privileges.ADMIN_MANAGE_BEATMAPS
        & ~privileges.USER_DONOR
        & ~privileges.USER_PREMIUM
    )
    await user_utils.set_privileges(targetID, new_privileges)
    await user_utils.remove_user_badge(targetID, badges.BEATMAP_NOMINATION)
    await user_utils.add_user_badge(targetID, badges.ALUMNI)
    await user_utils.set_absolute_donor_expiry_time(targetID, 0)
    target_tokens = await osuToken.get_all_tokens_by_user_id(targetID)
    for token in target_tokens:
        await osuToken.update_token(token["token_id"], privileges=new_privileges)
    return f"{fro} has removed BN from {username}."


@command(trigger="!faq", syntax="<name>")
async def faq(fro: str, chan: str, message: list[str]) -> str:
    """Fetch a given FAQ response."""
    key = message[0].lower()
    res = await glob.db.fetch("SELECT callback FROM faq WHERE name = %s", [key])
    if res is None:
        return "No FAQ topics could be found by that name."
    return res["callback"]  # type: ignore[no-any-return]


MAX_ROLL = 1_000_000


@command(trigger="!roll")
async def roll(fro: str, chan: str, message: list[str]) -> str:
    if message and message[0].isnumeric():
        maxPoints = min(int(message[0]), MAX_ROLL)
    else:
        maxPoints = 100

    points = random.randrange(maxPoints)
    return f"{fro} rolls {points} points!"


@command(trigger="!alertall", privs=privileges.ADMIN_SEND_ALERTS, hidden=True)
async def alertall(fro: str, chan: str, message: list[str]) -> str:
    """Send a notification message to all users."""
    if not (msg := " ".join(message).strip()):
        return "Guy was going to say @everyone and leave..."

    userID = await user_utils.get_id_from_username(fro)
    await stream_messages.broadcast_data("main", serverPackets.notification(msg))
    await audit_logs.send_log(
        userID,
        f"has sent an alert to all users: '{msg}'",
    )
    await audit_logs.send_log_as_discord_webhook(
        message=f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) has sent an alert to all users:```\n{msg}```",
        discord_channel="ac_general",
    )
    return "Sent an alert to every online player."


@command(
    trigger="!alertu",
    privs=privileges.ADMIN_SEND_ALERTS,
    syntax="<username> <message>",
    hidden=True,
)
async def alertUser(fro: str, chan: str, message: list[str]) -> str | None:
    """Send a notification message to a specific user."""
    if not (msg := " ".join(message[1:]).strip()):
        return None

    target = message[0].lower()
    if not (targetID := await user_utils.get_id_from_username(target)):
        return "Could not find user."

    if not (targetToken := await osuToken.get_token_by_user_id(targetID)):
        return "User offline"

    await osuToken.enqueue(targetToken["token_id"], serverPackets.notification(msg))
    return f"Sent an alert to {target} ({targetID})."


@command(trigger="!moderated", privs=privileges.ADMIN_CHAT_MOD, hidden=True)
async def moderated(fro: str, channel_name: str, message: list[str]) -> str:
    """Set moderated mode for the current channel."""
    try:
        # Make sure we are in a channel and not PM
        if not channel_name.startswith("#"):
            raise exceptions.moderatedPMException

        # Get on/off
        enable = True
        if len(message) >= 1:
            if message[0] == "off":
                enable = False

        # Turn on/off moderated mode
        # NOTE: this will raise exceptions.channelUnknownException if the channel doesn't exist
        await channelList.updateChannel(channel_name, moderated=enable)
        userID = await user_utils.get_id_from_username(fro)
        await audit_logs.send_log(
            userID,
            f"has toggled moderated mode in {channel_name}.",
        )
        await audit_logs.send_log_as_discord_webhook(
            message=f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) has toggled moderated mode in {channel_name}.",
            discord_channel="ac_general",
        )
        response = (
            f'This channel is {"now" if enable else "no longer"} in moderated mode!'
        )
    except exceptions.channelUnknownException:
        response = "Channel doesn't exist."
    except exceptions.moderatedPMException:
        response = "You are trying to put a private chat in moderated mode.. Let that sink in for a second.."

    return response


@command(
    trigger="!kick",
    privs=privileges.ADMIN_KICK_USERS,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def kick(fro: str, chan: str, message: list[str]) -> str:
    """Kick a specified player from the server."""
    message = [m.lower() for m in message]
    target = message[0]
    reason = " ".join(message[1:])
    userID = await user_utils.get_id_from_username(fro)

    if not (targetID := await user_utils.get_id_from_username(target)):
        return "Could not find user"

    if not (tokens := await osuToken.get_all_tokens_by_user_id(targetID)):
        return "Target not online."

    if not reason:
        return "Please specify a reason for the kick!"

    for token in tokens:
        await osuToken.kick(token["token_id"])
        await audit_logs.send_log(userID, f"has kicked {target}")
        await audit_logs.send_log_as_discord_webhook(
            message="\n".join(
                [
                    f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) has kicked [{target}](https://akatsuki.gg/u/{targetID}) from the server.",
                    f"**Reason**: {reason}",
                    f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
                ],
            ),
            discord_channel="ac_general",
        )
    return f"{target} has been kicked from the server."


@command(
    trigger="!silence",
    privs=privileges.ADMIN_SILENCE_USERS,
    syntax="<target_name> <amount> <unit(s/m/h/d/w)> <reason>",
    hidden=True,
)
async def silence(fro: str, chan: str, message: list[str]) -> str:
    """Silence a specified player a specified amount of time."""
    message = [m.lower() for m in message]
    target = message[0]
    amount_input = message[1]
    unit = message[2]

    if not (reason := " ".join(message[3:]).strip()):
        return "Please provide a valid reason."

    if not amount_input.isnumeric():
        return "The amount must be a number."
    else:
        amount = int(amount_input)

    # Make sure the user exists
    if not (targetID := await user_utils.get_id_from_username(target)):
        return f"{target}: user not found."

    userID = await user_utils.get_id_from_username(fro)

    # Calculate silence seconds
    if unit == "s":
        silenceTime = amount
    elif unit == "m":
        silenceTime = amount * 60
    elif unit == "h":
        silenceTime = amount * 3600
    elif unit == "d":
        silenceTime = amount * 86400
    elif unit == "w":
        silenceTime = amount * 604800
    else:
        return "Invalid time unit (s/m/h/d/w)."

    # Max silence time is 4 weeks
    if silenceTime > 0x24EA00:
        return "Invalid silence time. Max silence time is 4 weeks."

    # Send silence packet to target if he's connected
    targetToken = await osuToken.get_token_by_username(target)
    if targetToken:
        # user online, silence both in db and with packet
        await osuToken.silence(targetToken["token_id"], silenceTime, reason, userID)
    else:
        # User offline, silence user only in db
        await user_utils.silence(targetID, silenceTime, reason, userID)

    # Log message
    msg = f"{target} has been silenced for: {reason}."
    await audit_logs.send_log(userID, f"has silenced {target}")
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) silenced [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) silenced {target} for {reason}.",
    )

    return msg


@command(
    trigger="!unsilence",
    privs=privileges.ADMIN_SILENCE_USERS,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def unsilence(fro: str, chan: str, message: list[str]) -> str:
    """Unsilence a specified player."""
    message = [m.lower() for m in message]
    target = message[0]
    reason = " ".join(message[1:])

    # Make sure the user exists
    userID = await user_utils.get_id_from_username(fro)

    if not (targetID := await user_utils.get_id_from_safe_username(target)):
        return f"{target}: user not found."

    # Send new silence end packet to user if he's online
    if targetToken := await osuToken.get_token_by_user_id(targetID):
        # Remove silence in db and ingame
        await osuToken.silence(targetToken["token_id"], 0, "", userID)
    else:
        # Target offline, remove silence in db
        await user_utils.silence(targetID, 0, "", userID)
        await audit_logs.send_log(userID, f"has unsilenced {target}")
        await audit_logs.send_log_as_discord_webhook(
            message="\n".join(
                [
                    f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) unsilenced [{target}](https://akatsuki.gg/u/{targetID}).",
                    f"**Reason**: {reason}",
                    f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
                ],
            ),
            discord_channel="ac_general",
        )

    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) unsilenced {target} for {reason}",
    )
    return f"{target}'s silence reset."


@command(
    trigger="!ban",
    privs=privileges.ADMIN_MANAGE_PRIVILEGES,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def ban(fro: str, chan: str, message: list[str]) -> str:
    """Ban a specified player."""
    message = [m.lower() for m in message]
    target = message[0]
    reason = " ".join(message[1:])

    # Make sure the user exists
    if not (targetID := await user_utils.get_id_from_username(target)):
        return f"{target}: user not found."

    userID = await user_utils.get_id_from_username(fro)

    if not reason:
        return "Please specify a reason for the ban!"

    # Set allowed to 0
    await user_utils.ban(targetID)

    # Send ban packet to the user if he's online
    if targetToken := await osuToken.get_token_by_user_id(targetID):
        await osuToken.enqueue(targetToken["token_id"], serverPackets.loginBanned)

    await audit_logs.send_log(
        userID,
        f"has banned {target} ({targetID}) for {reason}",
    )
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) banned [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) banned for: {reason}",
    )
    return f"{target} has been banned."


@command(
    trigger="!unban",
    privs=privileges.ADMIN_MANAGE_PRIVILEGES,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def unban(fro: str, chan: str, message: list[str]) -> str:
    """Unban a specified player."""
    message = [m.lower() for m in message]
    target = message[0]
    reason = " ".join(message[1:])

    # Make sure the user exists
    if not (targetID := await user_utils.get_id_from_username(target)):
        return f"{target}: user not found."

    userID = await user_utils.get_id_from_username(fro)

    # Set allowed to 1
    await user_utils.unban(targetID)

    await audit_logs.send_log(userID, f"has unbanned {target}")
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) unbanned [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) unbanned for: {reason}",
    )
    return f"{target} has been unbanned."


@command(
    trigger="!restrict",
    privs=privileges.ADMIN_BAN_USERS,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def restrict(fro: str, chan: str, message: list[str]) -> str:
    """Restrict a specified player."""
    message = [m.lower() for m in message]
    target = message[0]
    reason = " ".join(message[1:])

    # Make sure the user exists
    if not (targetID := await user_utils.get_id_from_username(target)):
        return f"{target}: user not found."

    userID = await user_utils.get_id_from_username(fro)

    if not reason:
        return "Please specify a reason for the restriction!"

    # Put this user in restricted mode
    await user_utils.restrict(targetID)

    # Inform user of restriction if they're online
    if targetToken := await osuToken.get_token_by_user_id(targetID):
        await osuToken.notifyUserOfRestriction(targetToken["token_id"])

    await audit_logs.send_log(
        userID,
        f"has restricted {target} ({targetID}) for: {reason}",
    )
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) has restricted [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) restricted for: {reason}",
    )
    return f"{target} has been restricted."


@command(
    trigger="!unrestrict",
    privs=privileges.ADMIN_BAN_USERS,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def unrestrict(fro: str, chan: str, message: list[str]) -> str:
    """Unrestrict a specified player."""
    message = [m.lower() for m in message]
    target = message[0]
    reason = " ".join(message[1:])

    # Make sure the user exists
    if not (targetID := await user_utils.get_id_from_username(target)):
        return f"{target}: user not found."

    userID = await user_utils.get_id_from_username(fro)

    if not reason:
        return "Please specify a reason for the unrestriction!"

    await user_utils.unrestrict(targetID)

    await audit_logs.send_log(userID, f"has unrestricted {target}")
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) unrestricted [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) unrestricted for: {reason}",
    )
    return f"{target} has been unrestricted."


@command(
    trigger="!system reload",
    privs=privileges.ADMIN_MANAGE_SETTINGS,
    hidden=True,
)
async def systemReload(fro: str, chan: str, message: list[str]) -> str:
    """Reload the server's config."""
    await glob.banchoConf.reload()
    return "Bancho settings reloaded!"


@command(
    trigger="!system maintenance",
    privs=privileges.ADMIN_MANAGE_SETTINGS,
    hidden=True,
)
async def systemMaintenance(fro: str, chan: str, message: list[str]) -> str:
    """Set maintenance mode for the server."""
    # Turn on/off bancho maintenance
    maintenance = True

    # Get on/off
    if len(message) >= 2:
        if message[1] == "off":
            maintenance = False

    # Set new maintenance value in bancho_settings table
    await glob.banchoConf.setMaintenance(maintenance)

    if maintenance:
        # We have turned on maintenance mode
        # Users that will be disconnected
        who = []

        # Disconnect everyone but mod/admins
        for value in await osuToken.get_tokens():
            if not osuToken.is_staff(value["privileges"]):
                who.append(value["user_id"])

        await stream_messages.broadcast_data(
            "main",
            serverPackets.notification(
                " ".join(
                    [
                        "Akatsuki is currently in maintenance mode.",
                        "Please try to login again later.",
                    ],
                ),
            ),
        )

        await tokenList.multipleEnqueue(serverPackets.loginError, who)
        msg = "The server is now in maintenance mode!"
    else:
        # We have turned off maintenance mode
        # Send message if we have turned off maintenance mode
        msg = "The server is no longer in maintenance mode!"

    # Chat output
    return msg


@overload
async def getPPMessage(
    userID: int,
    just_data: Literal[False] = False,
) -> str | None: ...


@overload
async def getPPMessage(
    userID: int,
    just_data: Literal[True] = ...,
) -> Any: ...


async def getPPMessage(
    userID: int,
    just_data: bool = False,
) -> str | None | Any:
    if not (token := await osuToken.get_token_by_user_id(userID)):
        return None

    current_info = token["last_np"]
    if current_info is None:
        return None

    currentMap = current_info["beatmap_id"]
    currentMods = current_info["mods"]
    currentAcc = current_info["accuracy"]
    currentMode = 0
    currentMisscount = 0

    beatmap = await beatmaps_service.fetch_by_id(currentMap)
    if not beatmap:
        return "Could not retrieve beatmap information for the given map."

    if currentAcc == -1:
        performance_requests = [
            performance_utils.PerformanceRequest(
                beatmap_id=currentMap,
                beatmap_md5=beatmap.beatmap_md5,
                mode=currentMode,
                mods=currentMods,
                max_combo=beatmap.max_combo,
                accuracy=accuracy,
                miss_count=currentMisscount,
            )
            for accuracy in DEFAULT_PERFORMANCE_ACCURACY_VALUES
        ]
    else:
        performance_requests = [
            performance_utils.PerformanceRequest(
                beatmap_id=currentMap,
                beatmap_md5=beatmap.beatmap_md5,
                mode=currentMode,
                mods=currentMods,
                max_combo=beatmap.max_combo,
                accuracy=currentAcc,
                miss_count=currentMisscount,
            ),
        ]

    performance_results = await performance_utils.calculate_performance_batch(
        requests=performance_requests,
    )

    data: dict[str, Any] = {
        "status": 200,
        "message": "ok",
        "song_name": beatmap.song_name,
        "length": beatmap.hit_length,
        "stars": performance_results[0].stars,
        "ar": beatmap.ar,
        "bpm": beatmap.bpm,
    }
    if currentAcc == -1:
        data["pp"] = [result.pp for result in performance_results]
    else:
        data["pp"] = [performance_results[0].pp]

    if just_data:
        return data

    # Create a list of our message to be joined with ' ' at end.
    msg = [data["song_name"]]

    # Mods
    if currentMods:
        msg.append(f"+{scoreUtils.readableMods(currentMods)}")

    # PP List, either with general acc values, or specific acc.
    if currentAcc == -1:
        msg.append(
            " | ".join(
                [
                    f'95%: {data["pp"][3]:.2f}pp',
                    f'98%: {data["pp"][2]:.2f}pp',
                    f'99%: {data["pp"][1]:.2f}pp',
                    f'100%: {data["pp"][0]:.2f}pp',
                ],
            ),
        )
    else:
        msg.append(f'{currentAcc:.2f}%: {data["pp"][0]:.2f}pp')

    # BPM
    msg.append(f'| ♪ {data["bpm"]} |')

    # AR (with and without mods)
    if currentMods & mods.HARDROCK:
        msg.append(f'AR {min(10, data["ar"] * 1.4):.2f} ({data["ar"]:.2f})')
    elif currentMods & mods.EASY:
        msg.append(f'AR {max(0, data["ar"] / 2):.2f} ({data["ar"]:.2f})')
    else:
        msg.append(f'AR {data["ar"]}')

    # Star rating
    msg.append(f'| ★ {data["stars"]:.2f}')

    return " ".join(msg)


async def _get_beatmap_download_embed(beatmapID: int) -> str:
    if not (
        beatmap := await glob.db.fetch(
            "SELECT song_name, beatmapset_id "
            "FROM beatmaps WHERE beatmap_id = %s LIMIT 1",
            [beatmapID],
        )
    ):
        return "Sorry, I'm not able to provide a download link for this map :("

    return "[https://osu.ppy.sh/beatmapsets/{beatmapset_id} {song_name}]".format(
        **beatmap,
    )


@command(trigger="!mapdl", hidden=False)
async def mapdl(fro: str, chan: str, message: list[str]) -> str:
    """Get a download link for the beatmap in the current context (multi, spectator)."""
    try:
        match_id = await channelList.getMatchIDFromChannel(chan)
    except exceptions.wrongChannelException:
        match_id = None

    if match_id:
        multiplayer_match = await matchList.getMatchByID(match_id)
        assert multiplayer_match is not None
        beatmap_id = multiplayer_match["beatmap_id"]
    else:  # Spectator
        try:
            spectatorHostUserID = channelList.getSpectatorHostUserIDFromChannel(chan)
        except exceptions.wrongChannelException:
            return "This command is only usable when either spectating a user, or playing multiplayer."

        spectatorHostToken = await osuToken.get_token_by_user_id(spectatorHostUserID)
        if not spectatorHostToken:
            return "The spectator host is offline."

        beatmap_id = spectatorHostToken["beatmap_id"]

    return f"[Map Download] {await _get_beatmap_download_embed(beatmap_id)}"


@command(trigger="\x01ACTION is playing", hidden=True)
@command(trigger="\x01ACTION is editing", hidden=True)
@command(trigger="\x01ACTION is watching", hidden=True)
@command(trigger="\x01ACTION is listening to", hidden=True)
async def tillerinoNp(fro: str, chan: str, message: list[str]) -> str | None:
    # don't document this, don't want it showing up in !help
    if not (token := await osuToken.get_token_by_username(fro)):
        return None

    # MapDL trigger for #spect_
    if chan.startswith("#spect_"):
        spectatorHostUserID = channelList.getSpectatorHostUserIDFromChannel(chan)
        spectatorHostToken = await osuToken.get_token_by_user_id(
            spectatorHostUserID,
        )
        return (
            await _get_beatmap_download_embed(spectatorHostToken["beatmap_id"])
            if spectatorHostToken
            else None
        )

    npmsg = " ".join(message[1:])

    match = chatbot.NOW_PLAYING_REGEX.fullmatch(npmsg)
    if match is None:
        logger.error(
            "Error parsing /np message",
            extra={"chat_message": npmsg},
        )
        return "An error occurred while parsing /np message :/ - reported to devs"

    mods_int = 0
    if match["mods"] is not None:
        for _mods in match["mods"][1:].split(" "):
            mods_int |= mods.NP_MAPPING_TO_INTS[_mods]

    if match["bid"] is not None:
        # Get beatmap id from URL
        beatmap_id = int(match["bid"])
    else:
        # They /np'ed a map, but no beatmap id is present.
        # We'll need to use the set id to find a beatmap id.
        beatmapset_id = int(match["sid"])
        beatmap_rec = await glob.db.fetch(
            """
            SELECT beatmap_id
            FROM beatmaps
            WHERE beatmapset_id = %s
            ORDER BY beatmap_id DESC
            LIMIT 1
            """,
            [beatmapset_id],
        )
        if beatmap_rec is None:
            return "Could not find a beatmap for this beatmapset."

        beatmap_id = beatmap_rec["beatmap_id"]

    # Return tillerino message
    await osuToken.update_token(
        token["token_id"],
        last_np={
            "beatmap_id": beatmap_id,
            "mods": mods_int,
            "accuracy": -1.0,
        },
    )

    # Send back pp values only if this is a DM (not a public channel)
    if chan.startswith("#"):
        return None

    return await getPPMessage(token["user_id"])


@command(trigger="!with", syntax="<mods>", hidden=True)
async def tillerinoMods(fro: str, chan: str, message: list[str]) -> str | None:
    """Get the pp values for the last /np'ed map, with specified mods."""
    # Run the command in PM only
    if chan.startswith("#"):
        return None

    if not (token := await osuToken.get_token_by_username(fro)):
        return None

    if token["last_np"] is None:
        return "Please give me a beatmap first with /np command."

    # Check passed mods and convert to enum
    modMap = {
        "NF": mods.NOFAIL,
        "EZ": mods.EASY,
        "TS": mods.TOUCHSCREEN,
        "HD": mods.HIDDEN,
        "HR": mods.HARDROCK,
        "SD": mods.SUDDENDEATH,
        "DT": mods.DOUBLETIME,
        "RX": mods.RELAX,
        "HT": mods.HALFTIME,
        "NC": mods.NIGHTCORE | mods.DOUBLETIME,
        "FL": mods.FLASHLIGHT,
        "SO": mods.SPUNOUT,
        "AP": mods.AUTOPILOT,
        "PF": mods.PERFECT,
        "V2": mods.SCOREV2,
    }

    _mods = 0

    for m in (message[0][i : i + 2].upper() for i in range(0, len(message[0]), 2)):
        if not (
            (m in ("DT", "NC") and _mods & mods.HALFTIME)
            or (m == "HT" and _mods & (mods.DOUBLETIME | mods.NIGHTCORE))
            or (m == "EZ" and _mods & mods.HARDROCK)
            or (m == "HR" and _mods & mods.EASY)
            or (m == "AP" and _mods & mods.RELAX)
            or (m == "RX" and _mods & mods.AUTOPILOT)
            or (m == "PF" and _mods & mods.SUDDENDEATH)
            or (m == "SD" and _mods & mods.PERFECT)
        ):
            _mods |= modMap.get(m, mods.NOMOD)

    # Set mods
    token["last_np"]["mods"] = _mods
    await osuToken.update_token(token["token_id"], last_np=token["last_np"])

    # Return tillerino message for that beatmap with mods
    return await getPPMessage(token["user_id"])


@command(trigger="!last", hidden=False)
async def tillerinoLast(fro: str, chan: str, message: list[str]) -> str | None:
    """Show information about your most recently submitted score."""
    if not (token := await osuToken.get_token_by_username(fro)):
        return None

    if token["autopilot"]:
        table = "scores_ap"
    elif token["relax"]:
        table = "scores_relax"
    else:
        table = "scores"

    if not (
        data := await glob.db.fetch(
            "SELECT {t}.*, b.song_name AS sn, "
            "b.beatmap_id AS bid, b.beatmapset_id as bsid, "
            "b.ranked, b.max_combo AS fc FROM {t} "
            "LEFT JOIN beatmaps b USING(beatmap_md5) "
            "LEFT JOIN users u ON u.id = {t}.userid "
            'WHERE u.username = "{f}" '
            "ORDER BY {t}.time DESC LIMIT 1".format(t=table, f=fro),
        )
    ):
        return "You'll need to submit a score first!"

    rank = (
        generalUtils.get_score_grade(
            game_mode=data["play_mode"],
            mods=data["mods"],
            accuracy=data["accuracy"],
            count_300s=data["300_count"],
            count_100s=data["100_count"],
            count_50s=data["50_count"],
            count_misses=data["misses_count"],
        )
        if data["completed"] != 0
        else "F"
    )

    combo = (
        "(FC)"
        if data["max_combo"] == data["fc"]
        else f'{data["max_combo"]:,}x/{data["fc"]:,}x'
    )

    msg = [f"{fro} |"] if chan == CHATBOT_USER_NAME else []
    msg.append("[https://osu.ppy.sh/beatmapsets/{bsid}#{bid} {sn}]".format(**data))

    if data["play_mode"] != gameModes.STD:
        msg.append(f'<{gameModes.getGamemodeFull(data["play_mode"])}>')

    if data["mods"]:
        msg.append(f'+{scoreUtils.readableMods(data["mods"])}')

    accuracy_expanded = " / ".join(
        str(i)
        for i in [
            data["300_count"],
            data["100_count"],
            data["50_count"],
            data["misses_count"],
        ]
    )

    # TODO: hook this up to always use performance-service
    stars = 0.0

    if data["play_mode"] != gameModes.CTB:
        if data["mods"]:
            await osuToken.update_token(
                token["token_id"],
                last_np={
                    "beatmap_id": data["bid"],
                    "mods": data["mods"],
                    "accuracy": data["accuracy"],
                },
            )
            oppaiData = await getPPMessage(token["user_id"], just_data=True)
            if isinstance(oppaiData, str):
                return oppaiData  # error

            if "stars" in oppaiData:
                stars = oppaiData["stars"]

        if data["ranked"] == 5:
            pp_or_score = f"{data['score']:,}"
        else:
            pp_or_score = f"{data['pp']:,.2f}pp"

        msg.append(
            " | ".join(
                [
                    f'({data["accuracy"]:.2f}%, {rank.upper()}) {combo}',
                    f"{pp_or_score}, ★ {stars:.2f}",
                    f"{{ {accuracy_expanded} }}",
                ],
            ),
        )
    else:  # CTB has specific stuff
        msg.append(
            " | ".join(
                [
                    f'({data["accuracy"]:.2f}%, {rank.upper()}) {combo}',
                    f'{data["score"]:,}, ★ {stars:.2f}',
                    f"{{ {accuracy_expanded} }}",
                ],
            ),
        )

    return " ".join(msg)


@command(trigger="!report", hidden=True)
async def report(fro: str, chan: str, message: list[str]) -> None:
    """Report a player with a given message - your message will be hidden."""
    msg = ""
    try:  # TODO: Rate limit
        # Make sure the message matches the regex
        if not (result := chatbot.REPORT_REGEX.search(" ".join(message))):
            raise exceptions.invalidArgumentsException()

        # Get username, report reason and report info
        target, reason, additionalInfo = result.groups()
        userID = await user_utils.get_id_from_username(fro)
        targetID = await user_utils.get_id_from_username(target)

        # Make sure the user exists
        if not targetID:
            raise exceptions.userNotFoundException()

        # Make sure the target is not chatbot
        if targetID == CHATBOT_USER_ID:
            raise exceptions.invalidUserException()

        # Make sure that the user has specified additional info if report reason is 'Other'
        if reason.lower() == "other" and not additionalInfo:
            raise exceptions.missingReportInfoException()

        # Get the token if possible
        chatlog = ""
        if token := await osuToken.get_token_by_user_id(targetID):
            chatlog = await osuToken.getMessagesBufferString(token["token_id"])

        # Everything is fine, submit report
        await glob.db.execute(
            "INSERT INTO reports (id, from_uid, to_uid, reason, chatlog, time) VALUES (NULL, %s, %s, %s, %s, %s)",
            [
                await user_utils.get_id_from_username(fro),
                targetID,
                f"{reason} - ingame {f'({additionalInfo})' if additionalInfo else ''}",
                chatlog,
                int(time.time()),
            ],
        )

        msg = f"You've reported {target} for {reason}{f'({additionalInfo})' if additionalInfo else ''}. An Administrator will check your report as soon as possible. Every !report message you may see in chat wasn't sent to anyone, so nobody in chat, but admins, know about your report. Thank you for reporting!"

        # Log report to discord
        await audit_logs.send_log_as_discord_webhook(
            message="\n".join(
                [
                    f"[{fro}](https://akatsuki.gg/u/{userID}) reported [{target}](https://akatsuki.gg/u/{targetID}) ({targetID}) for {reason}.",
                    f"**Additional Info**: ({additionalInfo})",
                    f"\n> :gear: [View all reports](https://old.akatsuki.gg/index.php?p=126) on **Admin Panel**.",
                ],
            ),
            discord_channel="ac_general",
        )
    except exceptions.invalidUserException:
        msg = "Aika would never upset you like that - maybe you had the wrong person?"
    except exceptions.invalidArgumentsException:
        msg = "Invalid report command syntax. To report an user, click on it and select 'Report user'."
    except exceptions.userNotFoundException:
        msg = "The user you've tried to report doesn't exist."
    except exceptions.missingReportInfoException:
        msg = "Please specify the reason of your report."
    except:
        raise
    finally:
        if not msg:
            return

        token = await osuToken.get_token_by_username(fro)
        if token is None:
            return

        await osuToken.enqueue(
            token["token_id"],
            serverPackets.notification(msg),
        )


@command(
    trigger="!freeze",
    privs=privileges.ADMIN_FREEZE_USERS,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def freeze(fro: str, chan: str, message: list[str]) -> str:
    """Freeze a specified player."""
    target = message[0].lower()
    reason = " ".join(message[1:])

    if not (targetID := await user_utils.get_id_from_username(target)):
        return "That user does not exist"

    if await user_utils.get_freeze_restriction_date(targetID):
        return "That user is already frozen."

    if not reason:
        return f"Please specify your reason to freeze {target}."

    await user_utils.freeze(
        targetID,
        author_user_id=await user_utils.get_id_from_username(fro),
    )
    userID = await user_utils.get_id_from_username(fro)
    await audit_logs.send_log(userID, f"has froze {target}")
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) froze [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(targetID, f"{fro} ({userID}) froze {target}")
    return f"Froze {target}."


@command(
    trigger="!unfreeze",
    privs=privileges.ADMIN_FREEZE_USERS,
    syntax="<target_name> <reason>",
    hidden=True,
)
async def unfreeze(fro: str, chan: str, message: list[str]) -> str:
    """Unfreeze a specified player."""
    target = message[0].lower()
    reason = " ".join(message[1:])

    if not (targetID := await user_utils.get_id_from_username(target)):
        return "That user does not exist"

    if not await user_utils.get_freeze_restriction_date(targetID):
        return "That user is not frozen."

    if not reason:
        return f"Please specify a reason to unfreeze {target}."

    userID = await user_utils.get_id_from_username(fro)
    await user_utils.unfreeze(targetID, author_user_id=userID)
    await audit_logs.send_log(userID, f"unfroze {target}")
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) unfroze [{target}](https://akatsuki.gg/u/{targetID}).",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )

    await user_utils.append_cm_notes(targetID, f"{fro} ({userID}) unfroze {target}")
    return f"Unfroze {target}."


@command(
    trigger="!map",
    privs=privileges.ADMIN_MANAGE_BEATMAPS,
    syntax="<rank/love/unrank> <set/map>",
    hidden=True,
)
async def editMap(fro: str, chan: str, message: list[str]) -> str | None:
    """Edit the ranked status of the last /np'ed map."""
    # Rank, unrank, and love maps with a single command.
    # Syntax: /np
    #         !map <rank/unrank/love> <set/map>
    message = [m.lower() for m in message]

    if not (token := await osuToken.get_token_by_username(fro)):
        return None

    if token["last_np"] is None:
        return "Please give me a beatmap first with /np command."

    if message[0] not in {"rank", "unrank", "love"}:
        return "Status must be either rank, unrank, or love."

    if message[1] not in {"set", "map"}:
        return "Scope must either be set or map."

    status_to_int: Callable[[str], int] = lambda s: {
        "love": 5,
        "rank": 2,
        "unrank": 0,
    }[s]
    status_to_readable: Callable[[int], str] = lambda s: {
        5: "Loved",
        2: "Ranked",
        0: "Unranked",
    }[s]

    status = status_to_int(message[0])
    status_readable = status_to_readable(status)

    is_set = message[1] == "set"
    scope = "beatmapset_id" if is_set else "beatmap_id"

    if not (
        res := await glob.db.fetch(  # bsid is needed for dl link so we need it either way
            "SELECT `ranked`, `beatmapset_id`, `song_name`, `mode`, `max_combo`, `hit_length`, `ar`, `od`, `bpm`"
            "FROM `beatmaps` WHERE `beatmap_id` = %s",
            [token["last_np"]["beatmap_id"]],
        )
    ):
        return "Could not find beatmap."

    if res["ranked"] == status:
        return f"That map is already {status_readable.lower()}."

    rank_id = res["beatmapset_id"] if is_set else token["last_np"]["beatmap_id"]

    # Fix relax scores on the map (if going to/coming from loved).
    # Due to the way we handle loved on relax, this is nescessary.
    if status == 5 or res["ranked"] == 5:
        # Get all md5's from maps we need to change.
        beatmap_md5s = await glob.db.fetchAll(
            "SELECT beatmap_md5, beatmap_id FROM beatmaps "
            f"WHERE {scope} = %s AND ranked = %s",
            [rank_id, res["ranked"]],
        )

    # Update map's ranked status.
    await glob.db.execute(
        "UPDATE beatmaps SET ranked = %s, ranked_status_freezed = 1, "
        f"rankedby = {token['user_id']} WHERE {scope} = %s",
        [status, rank_id],
    )

    beatmap_md5s = await glob.db.fetchAll(
        f"SELECT beatmap_md5 FROM beatmaps WHERE {scope} = %s",
        [rank_id],
    )
    assert beatmap_md5s is not None

    async with glob.redis.pipeline() as pipe:
        for md5 in beatmap_md5s:
            await pipe.publish("cache:map_update", f"{md5['beatmap_md5']},{status}")

        await pipe.execute()

    # Service logos as emojis
    icon_akatsuki = "<:akatsuki:1253876231645171814>"

    # osu! game mode emoji dictionary
    mode_to_emoji: Callable[[int], str] = lambda s: {
        3: "<:modemania:1087863868782547014>",
        2: "<:modefruits:1087863938982612994>",
        1: "<:modetaiko:1087863916278853662>",
        0: "<:modeosu:1087863892308410398>",
    }[s]

    # Colour & icons used in stable client
    status_to_colour: Callable[[int], int] = lambda s: {
        5: 0xFF66AA,
        2: 0x6BCEFF,
        0: 0x696969,
    }[s]
    status_to_emoji_id: Callable[[int], str] = lambda s: {
        5: "1166976753869279272",
        2: "1166976760424964126",
        0: "1166976756230651934",
    }[s]

    # Get previous status (for emojis too, pretty icons...)
    prev_status_readable = status_to_readable(res["ranked"])
    prev_status_emoji_id = status_to_emoji_id(res["ranked"])

    # Get the nominator profile URL just once
    nominator_profile_url = user_utils.get_profile_url(token["user_id"])

    # Get the last /np'd Beatmap ID
    last_np_map_id = token["last_np"]["beatmap_id"]

    webhook = discord.Webhook(
        url=settings.WEBHOOK_NOW_RANKED,
        colour=status_to_colour(status),
        author=f"{fro} ({token['user_id']})",
        author_url=f"{nominator_profile_url}",
        author_icon=f"https://a.akatsuki.gg/{token['user_id']}",
        title=f'{mode_to_emoji(res["mode"])} {res["song_name"]}',
        title_url=f"https://beatmaps.akatsuki.gg/api/d/{res['beatmapset_id']}",
        desc=f'### This {message[1]} has received a status update. 📝\n**Length**: `{generalUtils.secondsToReadable(res["hit_length"])}` **BPM**: `{res["bpm"]}`\n**AR**: `{res["ar"]}` **OD**: `{res["od"]}` **Combo**: `{res["max_combo"]}x`',
        fields=[
            {"name": k, "value": v}
            for k, v in {
                "Previous Status": f"<:{prev_status_readable}:{prev_status_emoji_id}>・{prev_status_readable}",
                "Leaderboard": f"\n{icon_akatsuki}・[`Akatsuki`](https://akatsuki.gg/s/{res['beatmapset_id']})",
            }.items()
        ],
        image=f'https://assets.ppy.sh/beatmaps/{res["beatmapset_id"]}/covers/cover.jpg?1522396856',
        thumbnail=f"https://cdn.discordapp.com/emojis/{status_to_emoji_id(status)}.png",
    )
    job_scheduling.schedule_job(webhook.post())

    if is_set:
        beatmap_url = (
            f'beatmap set [https://osu.ppy.sh/beatmapsets/{rank_id} {res["song_name"]}]'
        )
    else:
        beatmap_url = (
            f'beatmap [https://osu.ppy.sh/beatmaps/{rank_id} {res["song_name"]}]'
        )

    chatbot_token = await osuToken.get_token_by_user_id(CHATBOT_USER_ID)
    assert chatbot_token is not None
    await chat.send_message(
        sender_token_id=chatbot_token["token_id"],
        recipient_name="#announce",
        message=f"[{nominator_profile_url} {fro}] has {status_readable} {beatmap_url}",
    )
    return "Success - it can take up to 60 seconds to see a change on the leaderboards (due to caching limitations)."


@command(
    trigger="!announce",
    privs=privileges.ADMIN_SEND_ALERTS,
    syntax="<announcement>",
    hidden=True,
)
async def postAnnouncement(fro: str, chan: str, message: list[str]) -> str:
    """Send a message to the #announce channel."""
    chatbot_token = await osuToken.get_token_by_user_id(CHATBOT_USER_ID)
    assert chatbot_token is not None

    messaging_error = await chat.send_message(
        sender_token_id=chatbot_token["token_id"],
        recipient_name="#announce",
        message=" ".join(message),
    )
    if messaging_error is not None:
        return "Failed to send announcement"

    return "Announcement successfully sent."


@command(
    trigger="!whitelist",
    privs=privileges.ADMIN_MANAGE_USERS,
    syntax="<target_name> <bit> <reason>",
    hidden=True,
)
async def editWhitelist(fro: str, chan: str, message: list[str]) -> str:
    """Edit the whitelisted status of a specified user."""
    message = [m.lower() for m in message]
    target = message[0]
    bit = int(message[1]) if message[1].isnumeric() else -1
    reason = " ".join(message[2:])

    if bit not in range(4):
        return "Invalid bit."

    if not (targetID := await user_utils.get_id_from_username(target)):
        return "That user does not exist."

    if not reason:
        return "Please specify the reason for your whitelist request."

    # Get command executioner's ID
    userID = await user_utils.get_id_from_username(fro)

    # If target user is online, update their token's whitelist bit
    targetToken = await osuToken.get_token_by_user_id(targetID)
    if targetToken is not None:
        targetToken = await osuToken.update_token(
            token_id=targetToken["token_id"],
            whitelist=bit,
        )

    await user_utils.update_whitelist_status(targetID, bit)

    await audit_logs.send_log(
        userID,
        f"has set {target}'s Whitelist Status to {bit} for {reason}",
    )
    await audit_logs.send_log_as_discord_webhook(
        message="\n".join(
            [
                f"[{fro}](https://akatsuki.gg/u/{userID}) ({userID}) has set [{target}](https://akatsuki.gg/u/{targetID})'s **Whitelist Status** to **{bit}**.",
                f"**Reason**: {reason}",
                f"\n> :bust_in_silhouette: [View this user](https://old.akatsuki.gg/index.php?p=103&id={targetID}) on **Admin Panel**.",
            ],
        ),
        discord_channel="ac_general",
    )
    await user_utils.append_cm_notes(
        targetID,
        f"{fro} ({userID}) has set {target}'s Whitelist Status to {bit}. Reason: {reason}",
    )
    return f"{target}'s Whitelist Status has been set to {bit}."


@command(trigger="!whoranked", hidden=True)
async def getMapNominator(fro: str, chan: str, message: list[str]) -> str | None:
    """Get the nominator for the last /np'ed map."""
    if not (token := await osuToken.get_token_by_username(fro)):
        return None

    if token["last_np"] is None:
        return "Please give me a beatmap first with /np command."

    if not (res := await user_utils.get_map_nominator(token["last_np"]["beatmap_id"])):
        return "That map isn't stored in our database."
    elif not res["rankedby"]:
        return "Our logs sadly do not go back far enough to find this data."

    status_readable = {0: "unranked", 2: "ranked", 5: "loved"}[res["ranked"]]
    rankedby = await user_utils.get_profile_url_osu_chat_embed(res["rankedby"])

    return f'{res["song_name"]} was {status_readable} by: {rankedby}.'


@command(trigger="!overwrite", hidden=True)
async def overwriteLatestScore(fro: str, chan: str, message: list[str]) -> str:
    """Force your latest score to overwrite. (NOTE: this is possibly destructive)"""
    userID = await user_utils.get_id_from_username(
        fro,
    )
    user_privs = await user_utils.get_privileges(userID)

    if not user_privs & privileges.USER_DONOR:
        return "The overwrite command is only available to Akatsuki supporters."

    if not (ratelimit := await user_utils.get_remaining_overwrite_wait(userID)):
        await glob.db.execute(
            "UPDATE users SET previous_overwrite = 1 WHERE id = %s",
            [userID],
        )
        return "\n".join(
            [
                f"{fro}: Since this is your first time using this command, I'll give a brief description.",
                "This command allows you to force your most recent score to overwrite any previous scores you had on the map.",
                "For example, say you just set some cool EZ score but you already had a nomod fc, and it didnt overwrite, you can use this to force it to overwrite the previous score.",
                "The command has now been unlocked.",
            ],
        )

    # Only allow the user to run it once / 10s.
    _time = int(time.time())

    if ratelimit > _time - 10:
        return f"This command can only be run every 10 seconds (Cooldown: {10 - (_time - ratelimit)}s)."

    if not (overwrite := await scoreUtils.overwritePreviousScore(userID)):
        return "It seems you don't have any scores.. Did you purchase supporter before setting a score? owo.."

    return f"Your score on {overwrite} has been overwritten."


@command(trigger="!mp", syntax="<subcommand>", hidden=False)
async def multiplayer(fro: str, chan: str, message: list[str]) -> str | None:
    """Contains many multiplayer subcommands (TODO: document them as well)."""

    _user_token = await osuToken.get_token_by_username(fro)
    if _user_token is None:
        return None

    if (
        chan != "#multiplayer"
        and not chan.startswith("#mp_")
        and chan.lower() != CHATBOT_USER_NAME.lower()
    ):
        return None  # command used only on #multiplayer channels or bot PMs

    async def mpAddReferee(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp addref <user>",
            )

        username = message[1].strip()
        if not username:
            raise exceptions.invalidArgumentsException("Please provide a username")

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.userNotFoundException("No such user")

        await match.add_referee(multiplayer_match["match_id"], target_token["user_id"])
        return f"Added {target_token['username']} to referees"

    async def mpRemoveReferee(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp addref <user>",
            )

        username = message[1].strip()
        if not username:
            raise exceptions.invalidArgumentsException("Please provide a username")

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.userNotFoundException("No such user")

        await match.remove_referee(
            multiplayer_match["match_id"],
            target_token["user_id"],
        )
        return f"Removed {target_token['username']} from referees"

    async def mpListReferees(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        ref_usernames: list[str] = []
        for ref_id in referees:
            username = await user_utils.get_username_from_id(ref_id)
            assert username is not None
            ref_usernames.append(username)

        refs = ", ".join(ref_usernames)
        return f"Referees for this match: {refs}"

    async def mpMake(user_token: osuToken.Token) -> str | None:
        if user_token["match_id"] is not None:
            return "You are already in a match."

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp make <name>.",
            )

        if not (user_token["privileges"] & privileges.USER_TOURNAMENT_STAFF):
            return "Only tournament staff may use this command"

        match_name = " ".join(message[1:]).strip()
        if not match_name:
            raise exceptions.invalidArgumentsException("Match name must not be empty!")

        multiplayer_match = await matchList.createMatch(
            match_name,
            match_password=secrets.token_hex(16),
            beatmap_id=0,
            beatmap_name="Tournament",
            beatmap_md5="",
            game_mode=0,
            host_user_id=-1,
            is_tourney=True,
        )

        await osuToken.joinMatch(
            user_token["token_id"],
            multiplayer_match["match_id"],
        )

        await match.setHost(multiplayer_match["match_id"], user_token["user_id"])
        await match.sendUpdates(multiplayer_match["match_id"])

        return f"Tourney match #{multiplayer_match['match_id']} created!"

    async def mpClose(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        await matchList.disposeMatch(multiplayer_match["match_id"])
        return (
            f"Multiplayer match #{multiplayer_match['match_id']} disposed successfully."
        )

    async def mpLock(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            await match.update_match(multiplayer_match["match_id"], is_locked=True)

        return "This match has been locked."

    async def mpUnlock(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            await match.update_match(multiplayer_match["match_id"], is_locked=False)

        return "This match has been unlocked."

    async def mpSize(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if (
            len(message) < 2
            or not message[1].isnumeric()
            or not 2 <= int(message[1]) <= 16
        ):
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp size <slots(2-16)>.",
            )
        matchSize = int(message[1])

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            await match.forceSize(multiplayer_match["match_id"], matchSize)

        return f"Match size changed to {matchSize}."

    async def mpMove(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if (
            len(message) < 3
            or not message[2].isnumeric()
            or int(message[2]) < 0
            or int(message[2]) > 16
        ):
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp move <username> <slot>.",
            )

        username = message[1]
        newSlotID = int(message[2])

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.userNotFoundException("No such user.")

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            success = await match.userChangeSlot(
                multiplayer_match["match_id"],
                target_token["user_id"],
                newSlotID,
            )

            maybe_token = await osuToken.update_token(
                target_token["token_id"],
                match_slot_id=newSlotID,
            )
            assert maybe_token is not None
            target_token = maybe_token

        return (
            f"{target_token['username']} moved to slot {newSlotID}."
            if success
            else "You can't use that slot: it's either already occupied by someone else or locked."
        )

    async def mpHost(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp host <username>.",
            )

        username = message[1].strip()
        if not username:
            raise exceptions.invalidArgumentsException("Please provide a username.")

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.userNotFoundException("No such user.")

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            success = await match.setHost(
                multiplayer_match["match_id"],
                target_token["user_id"],
            )

        return (
            f"{target_token['username']} is now the host"
            if success
            else f"Couldn't give host to {username}."
        )

    async def mpClearHost(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            await match.removeHost(multiplayer_match["match_id"], rm_referee=False)

        return "Host has been removed from this match."

    async def mpStart(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async def _start() -> bool:
            assert multiplayer_match is not None

            chatbot_token = await osuToken.get_token_by_user_id(CHATBOT_USER_ID)
            assert chatbot_token is not None
            if not await match.start(multiplayer_match["match_id"]):
                await chat.send_message(
                    sender_token_id=chatbot_token["token_id"],
                    recipient_name=chan,
                    message=(
                        "Couldn't start match. Make sure there are enough players and "
                        "teams are valid. The match has been unlocked."
                    ),
                )
                return True  # Failed to start
            else:
                await chat.send_message(
                    sender_token_id=chatbot_token["token_id"],
                    recipient_name=chan,
                    message="Have fun!",
                )

                if glob.amplitude is not None:
                    amplitude_event_props = {
                        "match": {
                            "match_id": multiplayer_match["match_id"],
                            "match_name": multiplayer_match["match_name"],
                            # "match_password": multiplayer_match["match_password"],
                            "beatmap_id": multiplayer_match["beatmap_id"],
                            "beatmap_name": multiplayer_match["beatmap_name"],
                            "beatmap_md5": multiplayer_match["beatmap_md5"],
                            "game_mode": multiplayer_match["game_mode"],
                            "host_user_id": multiplayer_match["host_user_id"],
                            "mods": multiplayer_match["mods"],
                            "match_scoring_type": multiplayer_match[
                                "match_scoring_type"
                            ],
                            "match_team_type": multiplayer_match["match_team_type"],
                            "match_mod_mode": multiplayer_match["match_mod_mode"],
                            "seed": multiplayer_match["seed"],
                            "is_tourney": multiplayer_match["is_tourney"],
                            "is_locked": multiplayer_match["is_locked"],
                            "is_starting": multiplayer_match["is_starting"],
                            "is_in_progress": multiplayer_match["is_in_progress"],
                            "creation_time": multiplayer_match["creation_time"],
                        },
                        "source": "bancho-service",
                    }

                    glob.amplitude.track(
                        BaseEvent(
                            event_type="start_multiplayer_match",
                            user_id=str(user_token["user_id"]),
                            device_id=user_token["amplitude_device_id"],
                            event_properties=amplitude_event_props,
                        ),
                    )

                return False

        async def _decreaseTimer(t: int) -> None:
            if t <= 0:
                await _start()
            else:
                if not t % 10 or t <= 5:
                    chatbot_token = await osuToken.get_token_by_user_id(CHATBOT_USER_ID)
                    assert chatbot_token is not None
                    await chat.send_message(
                        sender_token_id=chatbot_token["token_id"],
                        recipient_name=chan,
                        message=f"Match starts in {t} seconds.",
                    )

                loop = asyncio.get_running_loop()
                loop.call_later(
                    1.00,
                    lambda: job_scheduling.schedule_job(_decreaseTimer(t - 1)),
                )

        if len(message) < 2 or not message[1].isnumeric():
            startTime = 0
        else:
            startTime = int(message[1])

        force = len(message) > 1 and message[1].lower() == "force"

        # Force everyone to ready
        someoneNotReady = False
        slots = await slot.get_slots(multiplayer_match["match_id"])
        assert len(slots) == 16

        for slot_id, _slot in enumerate(slots):
            if _slot["status"] != slotStatuses.READY and _slot["user_token"]:
                someoneNotReady = True
                if force:
                    await match.toggleSlotReady(multiplayer_match["match_id"], slot_id)

        if someoneNotReady and not force:
            return (
                "Some users aren't ready yet. Use '!mp start force' "
                "if you want to start the match, even with non-ready players."
            )

        if not startTime:
            if await _start():
                return None
            return "Starting match"
        else:
            multiplayer_match = await match.update_match(
                multiplayer_match["match_id"],
                is_starting=True,
            )
            assert multiplayer_match is not None

            loop = asyncio.get_running_loop()
            loop.call_later(
                1.00,
                lambda: job_scheduling.schedule_job(_decreaseTimer(startTime - 1)),
            )

            return (
                f"Match starts in {startTime} seconds. The match has been locked. "
                "Please don't leave the match during the countdown "
                "or you might receive a penalty."
            )

    async def mp_abort_timer(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        await match.update_match(
            multiplayer_match["match_id"],
            is_timer_running=False,
        )
        return "Countdown stopped."

    async def mpInvite(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp invite <username>.",
            )

        username = message[1].strip()
        if not username:
            raise exceptions.invalidArgumentsException("Please provide a username.")

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.invalidUserException(
                "That user is not connected to Akatsuki right now.",
            )

        await match.invite(
            multiplayer_match["match_id"],
            sender_user_id=CHATBOT_USER_ID,
            recipient_user_id=target_token["user_id"],
        )

        await osuToken.enqueue(
            target_token["token_id"],
            serverPackets.notification(
                "Please accept the invite you've just received from "
                f"{CHATBOT_USER_NAME} to enter your tourney match.",
            ),
        )

        return f"An invite to this match has been sent to {username}."

    async def mpMap(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if (
            len(message) < 2
            or not message[1].isnumeric()
            or (len(message) == 3 and not message[2].isnumeric())
        ):
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp map <beatmapid> [<gamemode>].",
            )

        beatmapID = int(message[1])
        gameMode = int(message[2]) if len(message) == 3 else 0

        if gameMode < 0 or gameMode > 3:
            raise exceptions.invalidArgumentsException("Gamemode must be 0, 1, 2 or 3.")

        beatmapData = await glob.db.fetch(
            "SELECT song_name, beatmap_md5 FROM beatmaps WHERE beatmap_id = %s LIMIT 1",
            [beatmapID],
        )

        if beatmapData is None:
            raise exceptions.invalidArgumentsException(
                "The beatmap you've selected couldn't be found in the database. "
                "If the beatmap id is valid, please load the scoreboard first in "
                "order to cache it, then try again.",
            )

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            multiplayer_match = await match.update_match(
                multiplayer_match["match_id"],
                beatmap_id=beatmapID,
                beatmap_name=beatmapData["song_name"],
                beatmap_md5=beatmapData["beatmap_md5"],
                game_mode=gameMode,
            )
            assert multiplayer_match is not None

            await match.resetReady(multiplayer_match["match_id"])
            await match.sendUpdates(multiplayer_match["match_id"])

        return "Match map has been updated."

    async def mpSet(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if (
            len(message) < 2
            or not message[1].isnumeric()
            or (len(message) >= 3 and not message[2].isnumeric())
            or (len(message) >= 4 and not message[3].isnumeric())
        ):
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp set <teammode> [<scoremode>] [<size>].",
            )

        match_team_type = int(message[1])
        match_scoring_type = (
            int(message[2])
            if len(message) >= 3
            else multiplayer_match["match_scoring_type"]
        )

        if not 0 <= match_team_type <= 3:
            raise exceptions.invalidArgumentsException(
                "Match team type must be between 0 and 3.",
            )
        if not 0 <= match_scoring_type <= 3:
            raise exceptions.invalidArgumentsException(
                "Match scoring type must be between 0 and 3.",
            )

        oldMatchTeamType = multiplayer_match["match_team_type"]

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            multiplayer_match = await match.update_match(
                multiplayer_match["match_id"],
                match_team_type=match_team_type,
                match_scoring_type=match_scoring_type,
            )
            assert multiplayer_match is not None

            if (
                len(message) >= 4
                and message[3].isnumeric()
                and 2 <= int(message[3]) <= 16
            ):
                await match.forceSize(multiplayer_match["match_id"], int(message[3]))

            if multiplayer_match["match_team_type"] != oldMatchTeamType:
                await match.initializeTeams(multiplayer_match["match_id"])

            if (
                multiplayer_match["match_team_type"] == matchTeamTypes.TAG_COOP
                or multiplayer_match["match_team_type"] == matchTeamTypes.TAG_TEAM_VS
            ):
                multiplayer_match = await match.update_match(
                    multiplayer_match["match_id"],
                    match_mod_mode=matchModModes.NORMAL,
                )
                assert multiplayer_match is not None

            await match.sendUpdates(multiplayer_match["match_id"])

        return "Match settings have been updated!"

    async def mpAbort(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            await match.abort(multiplayer_match["match_id"])

        return "Match aborted!"

    async def mpKick(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp kick <username>.",
            )

        username = message[1].strip()
        if not username:
            raise exceptions.invalidArgumentsException("Please provide a username.")

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.userNotFoundException("No such user.")

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            slot_id = await match.getUserSlotID(
                multiplayer_match["match_id"],
                target_token["user_id"],
            )
            if not slot_id:
                raise exceptions.userNotFoundException(
                    "The specified user is not in this match.",
                )

            # toggle slot lock twice to kick the user
            for _ in range(2):
                await match.toggleSlotLocked(multiplayer_match["match_id"], slot_id)

        return f"{target_token['username']} has been kicked from the match."

    async def mpPassword(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            password = "" if len(message) < 2 or not message[1].strip() else message[1]
            await match.changePassword(multiplayer_match["match_id"], password)

        return "Match password has been changed!"

    async def mpRandomPassword(
        user_token: osuToken.Token,
    ) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            password = secrets.token_hex(16)
            await match.changePassword(multiplayer_match["match_id"], password)

        return "Match password has been randomized."

    async def mpMods(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) != 2 or len(message[1]) % 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp mods <mods, e.g. hdhr>",
            )

        modMap = {
            "NF": mods.NOFAIL,
            "EZ": mods.EASY,
            "TS": mods.TOUCHSCREEN,
            "HD": mods.HIDDEN,
            "HR": mods.HARDROCK,
            "SD": mods.SUDDENDEATH,
            "DT": mods.DOUBLETIME,
            "RX": mods.RELAX,
            "HT": mods.HALFTIME,
            "NC": mods.NIGHTCORE,
            "FL": mods.FLASHLIGHT,
            "SO": mods.SPUNOUT,
            "AP": mods.AUTOPILOT,
            "PF": mods.PERFECT,
            "V2": mods.SCOREV2,
        }

        _mods = 0
        freemods = False

        for m in (message[1][i : i + 2].upper() for i in range(0, len(message[1]), 2)):
            if m == "FM":
                freemods = True
            else:
                if not (
                    (m in {"DT", "NC"} and _mods & mods.HALFTIME)
                    or (m == "HT" and _mods & (mods.DOUBLETIME | mods.NIGHTCORE))
                    or (m == "EZ" and _mods & mods.HARDROCK)
                    or (m == "HR" and _mods & mods.EASY)
                    or (m == "RX" and _mods & mods.AUTOPILOT)
                    or (m == "AP" and _mods & mods.RELAX)
                    or (m == "PF" and _mods & mods.SUDDENDEATH)
                    or (m == "SD" and _mods & mods.PERFECT)
                ):
                    _mods |= modMap.get(m, 0)

        new_match_mod_mode = (
            matchModModes.FREE_MOD if freemods else matchModModes.NORMAL
        )

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            multiplayer_match = await match.update_match(
                multiplayer_match["match_id"],
                match_mod_mode=new_match_mod_mode,
            )
            assert multiplayer_match is not None

            await match.resetReady(multiplayer_match["match_id"])
            if multiplayer_match["match_mod_mode"] == matchModModes.FREE_MOD:
                await match.resetMods(multiplayer_match["match_id"])
            await match.changeMods(multiplayer_match["match_id"], _mods)

        return "Match mods have been updated!"

    async def mpTeam(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 3:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp team <username> <colour>.",
            )

        if (
            multiplayer_match["match_team_type"] != matchTeamTypes.TEAM_VS
            and multiplayer_match["match_team_type"] != matchTeamTypes.TAG_TEAM_VS
        ):
            return "Command only available in team vs."

        username = message[1].strip()
        if not username:
            raise exceptions.invalidArgumentsException("Please provide a username.")

        colour_dict = {"red": matchTeams.RED, "blue": matchTeams.BLUE}

        colour = message[2].lower().strip()
        colour_const = colour_dict.get(colour)

        if colour_const is None:
            raise exceptions.invalidArgumentsException(
                "Team colour must be red or blue.",
            )

        target_token = await osuToken.get_token_by_username(username)
        if not target_token:
            raise exceptions.userNotFoundException("No such user.")

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            await match.changeTeam(
                multiplayer_match["match_id"],
                target_token["user_id"],
                colour_const,
            )

        return f"{target_token['username']} is now in {colour} team"

    async def mpSettings(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        single = False if len(message) < 2 else message[1].strip().lower() == "single"
        msg: list[str] = ["PLAYERS IN THIS MATCH "]

        if not single:
            msg.append("(use !mp settings single for a single-line version):\n")
        else:
            msg.append(": ")

        empty = True
        slots = await slot.get_slots(multiplayer_match["match_id"])
        assert len(slots) == 16

        for _slot in slots:
            if _slot["user_token"] is None:
                continue

            readableStatuses = {
                slotStatuses.READY: "ready",
                slotStatuses.NOT_READY: "not ready",
                slotStatuses.NO_MAP: "no map",
                slotStatuses.PLAYING: "playing",
            }
            if _slot["status"] not in readableStatuses:
                readableStatus = "???"
            else:
                readableStatus = readableStatuses[_slot["status"]]
            empty = False
            slot_token = await osuToken.get_token(_slot["user_token"])
            assert slot_token is not None
            msg.append(
                "* [{team}] <{status}> ~ {username}{mods}{nl}".format(
                    team=(
                        "red"
                        if _slot["team"] == matchTeams.RED
                        else (
                            "blue"
                            if _slot["team"] == matchTeams.BLUE
                            else "!! no team !!"
                        )
                    ),
                    status=readableStatus,
                    username=slot_token["username"],
                    mods=(
                        f" (+ {scoreUtils.readableMods(_slot['mods'])})"
                        if _slot["mods"] > 0
                        else ""
                    ),
                    nl=" | " if single else "\n",
                ),
            )

        if empty:
            msg.append("Nobody.\n")

        return "".join(msg).rstrip(" | " if single else "\n")

    async def mpScoreV(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 2 or message[1] not in {"1", "2"}:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp scorev <1|2>.",
            )

        if message[1] == "2":
            new_scoring_type = matchScoringTypes.SCORE_V2
        else:
            new_scoring_type = matchScoringTypes.SCORE

        async with redisLock(match.make_lock_key(multiplayer_match["match_id"])):
            multiplayer_match = await match.update_match(
                multiplayer_match["match_id"],
                match_scoring_type=new_scoring_type,
            )
            assert multiplayer_match is not None

            await match.sendUpdates(multiplayer_match["match_id"])

        return f"Match scoring type set to scorev{message[1]}."

    async def mpHelp(_: osuToken.Token) -> str | None:
        return f"Supported multiplayer subcommands: <{' | '.join(subcommands.keys())}>."

    async def mp_link(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        return match.get_match_history_message(
            multiplayer_match["match_id"],
            multiplayer_match["match_history_private"],
        )

    async def mp_timer(user_token: osuToken.Token) -> str | None:
        if not user_token["match_id"]:
            return None

        multiplayer_match = await match.get_match(user_token["match_id"])
        if multiplayer_match is None:
            return None

        referees = await match.get_referees(multiplayer_match["match_id"])
        if user_token["user_id"] not in referees:
            return None

        if len(message) < 2:
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp timer <time (in seconds)>.",
            )

        if multiplayer_match["is_timer_running"]:
            return "A countdown is already running."

        if not message[1].isnumeric():
            raise exceptions.invalidArgumentsException(
                "Incorrect syntax: !mp timer <time (in seconds)>.",
            )
        countdown_time = int(message[1])

        if countdown_time < 1:
            raise exceptions.invalidArgumentsException(
                "Countdown time must be at least 1 second.",
            )

        if countdown_time > 300:  # 5 mins
            raise exceptions.invalidArgumentsException(
                "Countdown time must be less than 5 minutes.",
            )

        def _get_countdown_message(t: int, force: bool = False) -> str | None:
            minutes, seconds = divmod(t, 60)
            if minutes > 0 and not seconds:
                return f"Countdown ends in {minutes} minute(s)"

            _, unit_digit = divmod(seconds, 10)
            if force or (
                not minutes and seconds <= 30 and (not unit_digit or seconds <= 5)
            ):
                return f"Countdown ends in {seconds} second(s)"

            return None

        async def _decreaseTimer(t: int) -> None:
            chatbot_token = await osuToken.get_token_by_user_id(CHATBOT_USER_ID)
            assert chatbot_token is not None

            multi_match = await match.get_match(multiplayer_match["match_id"])
            if multi_match is None:
                return  # the match is gone

            if not multi_match["is_timer_running"]:
                return

            if t <= 0:
                await match.update_match(
                    multiplayer_match["match_id"],
                    is_timer_running=False,
                )
                await chat.send_message(
                    sender_token_id=chatbot_token["token_id"],
                    recipient_name=chan,
                    message=f"Countdown finished.",
                )
                return

            message = _get_countdown_message(t)
            if message:
                await chat.send_message(
                    sender_token_id=chatbot_token["token_id"],
                    recipient_name=chan,
                    message=message,
                )

            loop = asyncio.get_running_loop()
            loop.call_later(
                1.00,
                lambda: job_scheduling.schedule_job(_decreaseTimer(t - 1)),
            )

        await match.update_match(
            multiplayer_match["match_id"],
            is_timer_running=True,
        )

        loop = asyncio.get_running_loop()
        loop.call_later(
            1.00,
            lambda: job_scheduling.schedule_job(_decreaseTimer(countdown_time - 1)),
        )
        return _get_countdown_message(countdown_time, True)

    try:
        subcommands: dict[str, Callable[[osuToken.Token], Awaitable[str | None]]] = {
            "addref": mpAddReferee,
            "rmref": mpRemoveReferee,
            "listref": mpListReferees,
            "make": mpMake,
            "close": mpClose,
            "lock": mpLock,
            "unlock": mpUnlock,
            "size": mpSize,
            "move": mpMove,
            "host": mpHost,
            "clearhost": mpClearHost,
            "start": mpStart,
            "invite": mpInvite,
            "map": mpMap,
            "set": mpSet,
            "abort": mpAbort,
            "kick": mpKick,
            "password": mpPassword,
            "randompassword": mpRandomPassword,
            "mods": mpMods,
            "team": mpTeam,
            "settings": mpSettings,
            "scorev": mpScoreV,
            "help": mpHelp,
            "link": mp_link,
            "timer": mp_timer,
            "aborttimer": mp_abort_timer,
        }

        requestedSubcommand = message[0].lower().strip()
        if requestedSubcommand not in subcommands:
            raise exceptions.invalidArgumentsException("Invalid subcommand.")
        return await subcommands[requestedSubcommand](_user_token)
    except (
        exceptions.invalidArgumentsException,
        exceptions.userNotFoundException,
        exceptions.invalidUserException,
    ) as e:
        return str(e)
    except exceptions.wrongChannelException:
        return "This command only works in multiplayer chat channels."
    except exceptions.matchNotFoundException:
        return "Match not found."
    except:
        raise


USER_IDS_WHITELISTED_FOR_PY_COMMAND: set[int] = {1001, 1935}


@command(trigger="!py", privs=privileges.ADMIN_CAKER, hidden=False)
async def runPython(fro: str, chan: str, message: list[str]) -> str:
    userID = await user_utils.get_id_from_username(fro)
    if userID not in USER_IDS_WHITELISTED_FOR_PY_COMMAND:
        return "You are not allowed to use this command."

    # NOTE: not documented on purpose
    lines = " ".join(message).split(r"\n")
    definition = "\n ".join(["async def __py(fro, chan, message):"] + lines)

    try:
        exec(definition)  # define function
        ret = str(await locals()["__py"](fro, chan, message))  # run it
    except Exception as e:
        ret = f"{e.__class__}: {e}"

    return ret


# NOTE: the profiling commands must be run on the same k8s pod


@command(trigger="!profiling enable")
async def enable_profiling(fro: str, chan: str, message: list[str]) -> str:
    """Enable the profiler."""
    profiling.profiler.enable()
    return "Profiler enabled."


@command(trigger="!profiling disable")
async def disable_profiling(fro: str, chan: str, message: list[str]) -> str:
    """Disable the profiler."""
    profiling.profiler.disable()
    profiling.profiler.dump_stats("stats.dump")  # TODO: persist in s3
    return "Profiler disabled. Dumped stats to disk @ stats.dump"


@command(
    trigger="!leaderboardsize",
    syntax="<size>",
    hidden=True,
    privs=privileges.USER_PREMIUM,
)
async def leaderboard_size_cmd(fro: str, chan: str, message: list[str]) -> str:
    userID = await user_utils.get_id_from_username(fro)

    if not any(message):
        return "Please specify a new leaderboard size."

    size_str = message[0]
    if not size_str.isdigit():
        return "Invalid size!"

    size = int(size_str)
    if size > 1000:
        size = 1000
    elif size < 1:
        size = 1

    await user_utils.update_leaderboard_size(userID, size)
    return f"Your leaderboard size has been updated to {size}!"
