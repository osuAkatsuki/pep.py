from __future__ import annotations

import time
from time import localtime
from time import strftime
from typing import Any
from typing import TypedDict

import bcrypt

from common.constants import gameModes
from common.constants import privileges
from common.log import audit_logs
from common.log import logger
from constants import CHATBOT_USER_ID
from objects import glob


async def update_leaderboard_size(user_id: int, leaderboard_size: int) -> None:
    """
    Update a user's in-game leaderboard size preference.
    """

    await glob.db.execute(
        "UPDATE users SET leaderboard_size = %s WHERE id = %s",
        [leaderboard_size, user_id],
    )


async def get_playtime_total(user_id: int) -> int:
    """Get a users playtime for all gameModes combined."""

    res = await glob.db.fetch(
        """
        SELECT SUM(playtime) AS total_playtime
        FROM user_stats
        WHERE user_id = %s
        """,
        [user_id],
    )
    return res["total_playtime"] if res else 0  # type: ignore[no-any-return]


async def update_whitelist_status(user_id: int, new_value: int) -> None:
    """
    Update a user's whitelist status to the given new value.

    Value legend:
    0 = no whitelist
    1 = vanilla
    2 = relax
    3 = vanilla & relax
    """

    await glob.db.execute(
        "UPDATE users SET whitelist = %s WHERE id = %s",
        [new_value, user_id],
    )


class UserStatsResponse(TypedDict):
    ranked_score: int
    avg_accuracy: float
    playcount: int
    total_score: int
    pp: int
    global_rank: int


async def get_user_stats(
    user_id: int,
    game_mode: int,
    relax_ap: int,
) -> UserStatsResponse | None:
    """Get all user stats for the given game mode."""

    # Get stats
    stats = await glob.db.fetch(
        """
        SELECT ranked_score, avg_accuracy, playcount, total_score, pp
        FROM user_stats
        WHERE user_id = %s AND mode = %s
        """,
        [user_id, game_mode + (relax_ap * 4)],
    )
    if stats is None:
        logger.warning(
            "Stats row missing for user",
            extra={
                "user_id": user_id,
                "game_mode": game_mode,
                "relax_ap": relax_ap,
            },
        )
        return None

    global_rank = await get_global_rank(user_id, game_mode, relax_ap)

    return {
        "ranked_score": stats["ranked_score"],
        "avg_accuracy": stats["avg_accuracy"],
        "playcount": stats["playcount"],
        "total_score": stats["total_score"],
        "pp": stats["pp"],
        "global_rank": global_rank,
    }


async def get_id_from_safe_username(safe_username: str) -> int | None:
    """Get user ID from a safe username."""
    result = await glob.db.fetch(
        "SELECT id FROM users WHERE username_safe = %s",
        [safe_username],
    )
    return result["id"] if result else None


async def get_map_nominator(beatmap_id: int) -> Any | None:
    """Get the user who ranked a map by beatmapID."""
    res = await glob.db.fetch(
        "SELECT song_name, ranked, rankedby FROM beatmaps WHERE beatmap_id = %s",
        [beatmap_id],
    )
    return res if res else None


async def get_id_from_username(username: str) -> int:
    """
    Get username's user ID from user_id redis cache (if cache hit)
    or from db (and cache it for other requests) if cache miss.

    WARNING: returns `0` if the user is not found.
    """
    # TODO: Make this return Optional[int] and check for the
    # None case in all callers. Most are ignoring the 0 case rn.

    # Get user_id from redis
    usernameSafe: str = get_safe_username(username)
    user_id = await glob.redis.get(f"ripple:userid_cache:{usernameSafe}")

    if not user_id:
        # If it's not in redis, get it from mysql
        user_id = await get_id_from_safe_username(usernameSafe)

        # If it's invalid, return 0
        if not user_id:
            return 0

        # Otherwise, save it in redis and return it
        await glob.redis.set(
            f"ripple:userid_cache:{usernameSafe}",
            user_id,
            3600,
        )  # expires in 1 hour
        return user_id

    # Return userid from redis
    return int(user_id)


async def get_username_from_id(user_id: int) -> str | None:
    """Get a user's username by id."""

    result = await glob.db.fetch(
        "SELECT username FROM users WHERE id = %s",
        [user_id],
    )

    return result["username"] if result else None


async def authenticate(user_id: int, password: str) -> bool:
    """Check a user's login with specified password."""
    # Get saved password data
    passwordData = await glob.db.fetch(
        "SELECT password_md5 FROM users WHERE id = %s LIMIT 1",
        [user_id],
    )

    # Make sure the query returned something
    if not passwordData:
        return False

    pw_md5 = password.encode()
    db_pw_bcrypt = passwordData["password_md5"].encode()  # why is it called md5 LOL

    if db_pw_bcrypt in glob.bcrypt_cache:  # ~0.01ms
        return pw_md5 == glob.bcrypt_cache[db_pw_bcrypt]
    elif bcrypt.checkpw(pw_md5, db_pw_bcrypt):  # ~200ms
        glob.bcrypt_cache[db_pw_bcrypt] = pw_md5
        return True

    return False


async def get_user_pp_for_mode(
    user_id: int,
    game_mode: int,
    relax: bool,
    autopilot: bool,
) -> int:
    """Get a user's PP for the given game mode."""
    assert not (relax and autopilot)
    mode_offset = (4 if relax else 0) + (8 if autopilot else 0)
    result = await glob.db.fetch(
        """
        SELECT pp
        FROM user_stats
        WHERE user_id = %s
        AND mode = %s
        """,
        [user_id, game_mode + mode_offset],
    )
    return result["pp"] if result else 0  # type: ignore[no-any-return]


async def is_not_banned_or_restricted(user_id: int) -> bool:
    """Check if user is not banned or restricted."""
    return (
        await glob.db.fetch(
            "SELECT 1 FROM users WHERE id = %s AND privileges & 3 = 3",
            [user_id],
        )
        is not None
    )


async def is_restricted(user_id: int) -> bool:
    """Check if a user is restricted."""
    return (
        await glob.db.fetch(
            "SELECT 1 FROM users "
            "WHERE id = %s "
            "AND privileges & 1 = 0 "  # hidden profile
            "AND privileges & 2 != 0",  # has account access
            [user_id],
        )
        is not None
    )


async def is_banned(user_id: int) -> bool:
    """Check if a user is banned."""
    return (
        await glob.db.fetch(
            "SELECT 1 FROM users "
            "WHERE id = %s "
            "AND privileges & 3 = 0",  # no access, hidden profile
            [user_id],
        )
        is not None
    )


async def ban(user_id: int) -> None:
    """Ban a user."""
    # Set user as banned in db
    await glob.db.execute(
        "UPDATE users "
        "SET privileges = privileges & %s, "
        "ban_datetime = UNIX_TIMESTAMP() "
        "WHERE id = %s",
        [
            ~(
                privileges.USER_NORMAL
                | privileges.USER_PUBLIC
                # Seems appropriate to remove pending verification here
                | privileges.USER_PENDING_VERIFICATION
            ),
            user_id,
        ],
    )

    # Notify bancho about the ban
    await glob.redis.publish("peppy:ban", str(user_id))


async def unban(user_id: int) -> None:
    """Unban a user."""
    await glob.db.execute(
        "UPDATE users "
        "SET privileges = privileges | %s, "
        "ban_datetime = 0 "
        "WHERE id = %s",
        [privileges.USER_NORMAL | privileges.USER_PUBLIC, user_id],
    )

    await glob.redis.publish("peppy:unban", str(user_id))


async def restrict(user_id: int) -> None:
    """Restrict a user."""
    if await is_restricted(user_id):
        return

    # Set user as restricted in db
    await glob.db.execute(
        "UPDATE users SET privileges = privileges & %s, "
        "ban_datetime = UNIX_TIMESTAMP() WHERE id = %s",
        [~privileges.USER_PUBLIC, user_id],
    )

    # Notify bancho about this ban
    await glob.redis.publish("peppy:ban", str(user_id))


async def unrestrict(user_id: int) -> None:
    """Unrestrict a user by id. Functionally equivalent to calling unban()."""
    await unban(user_id)


async def append_cm_notes(
    user_id: int,
    notes: str,
    add_newline: bool = True,
    track_date: bool = True,
) -> None:
    """
    Append to a given user's "notes for community management".

    :param user_id: user id
    :param notes: text to append
    :param add_newline: if True, prepend \n to notes. Default: True.
    :param track_date: if True, prepend date and hour to the note. Default: True.
    :return:
    """

    if track_date:
        notes = f"[{strftime('%Y-%m-%d %H:%M:%S', localtime())}] {notes}"

    if add_newline:
        notes = f"\n{notes}"

    await glob.db.execute(
        "UPDATE users " 'SET notes = CONCAT(COALESCE(notes, ""), %s) ' "WHERE id = %s",
        [notes, user_id],
    )


async def get_privileges(user_id: int) -> int:
    """Return a user's privileges."""
    result = await glob.db.fetch(
        "SELECT privileges " "FROM users " "WHERE id = %s",
        [user_id],
    )

    return result["privileges"] if result else 0  # type: ignore[no-any-return]


async def get_freeze_restriction_date(user_id: int) -> int:
    """Return a user's enqueued restriction date."""
    result = await glob.db.fetch(
        "SELECT frozen FROM users WHERE id = %s",
        [user_id],
    )
    return result["frozen"] if result else 0  # type: ignore[no-any-return]


async def freeze(user_id: int, *, author_user_id: int = CHATBOT_USER_ID) -> None:
    """
    Enqueue a 'pending' restriction on a user. (7 days)

    Used for getting liveplays from users already suspected of cheating.
    """

    await begin_freeze_timer(user_id)  # to fix cron bugs

    author_name = await get_username_from_id(author_user_id)
    target_name = await get_username_from_id(user_id)

    await append_cm_notes(user_id, f"{author_name} ({author_user_id}) froze this user.")
    await audit_logs.send_log(author_user_id, f"froze {target_name} ({user_id}).")
    await audit_logs.send_log_as_discord_webhook(
        message=f"{author_name} has frozen [{target_name}](https://akatsuki.gg/u/{user_id}).",
        discord_channel="ac_general",
    )


async def begin_freeze_timer(user_id: int) -> int:
    """Enqueue a 'pending' restriction on a user. (in 7 days)"""
    restriction_time = int(time.time() + (86400 * 7))
    await glob.db.execute(
        "UPDATE users SET frozen = %s WHERE id = %s",
        [restriction_time, user_id],
    )
    return restriction_time


async def unfreeze(
    user_id: int,
    *,
    author_user_id: int = CHATBOT_USER_ID,
    should_log_to_cm_notes_and_discord: bool = True,
) -> None:
    """Dequeue a 'pending' restriction on a user."""

    await glob.db.execute(
        "UPDATE users SET frozen = 0 WHERE id = %s",
        [user_id],
    )

    if should_log_to_cm_notes_and_discord:
        author_name = await get_username_from_id(author_user_id)
        target_name = await get_username_from_id(user_id)

        await append_cm_notes(
            user_id,
            f"{author_name} ({author_user_id}) unfroze this user.",
        )
        await audit_logs.send_log(author_user_id, f"unfroze {target_name} ({user_id}).")
        await audit_logs.send_log_as_discord_webhook(
            message=f"{author_name} has unfrozen [{target_name}](https://akatsuki.gg/u/{user_id}).",
            discord_channel="ac_general",
        )


async def get_absolute_silence_end(user_id: int) -> int:
    """
    Get a user's **ABSOLUTE** silence end UNIX time.

    NOTE: Remember to subtract time.time() if you want to get remaining silence time.
    """
    rec = await glob.db.fetch(
        "SELECT silence_end FROM users WHERE id = %s",
        [user_id],
    )
    return rec["silence_end"] if rec else 0  # type: ignore[no-any-return]


async def get_remaining_silence_time(user_id: int) -> int:
    """
    Get a user's remaining silence time.

    NOTE: Returns 0 if the user is not silenced.
    """
    return max(0, await get_absolute_silence_end(user_id) - int(time.time()))


async def silence(
    user_id: int,
    seconds: int,
    silence_reason: str,
    author_user_id: int = CHATBOT_USER_ID,
) -> None:
    """Silence a user for a number of seconds for a given reason."""

    silence_time = int(time.time() + seconds)

    await glob.db.execute(
        "UPDATE users SET silence_end = %s, silence_reason = %s WHERE id = %s",
        [silence_time, silence_reason, user_id],
    )

    await audit_logs.send_log(
        author_user_id,
        (
            f'has silenced {await get_username_from_id(user_id)} for {seconds} seconds for the following reason: "{silence_reason}"'
            if seconds
            else f"has removed {await get_username_from_id(user_id)}'s silence"
        ),
    )


async def get_global_rank(user_id: int, game_mode: int, relax_ap: int) -> int:
    """Get user's global rank (eg: #1337) for a given game mode."""

    board = "leaderboard"
    if relax_ap == 1:
        board = "relaxboard"
    elif relax_ap == 2:
        board = "autoboard"

    position = await glob.redis.zrevrank(
        f"ripple:{board}:{gameModes.getGameModeForDB(game_mode)}",
        user_id,
    )

    return int(position) + 1 if position is not None else 0


async def get_friend_user_ids(user_id: int) -> list[int]:
    """Get a user's friendlist."""
    recs = await glob.db.fetchAll(
        "SELECT user2 FROM users_relationships WHERE user1 = %s",
        [user_id],
    )
    return [rec["user2"] for rec in recs]


async def add_friend(user_id: int, friend_user_id: int) -> None:
    """Create a new relationship between a given user and new friend."""

    # Make sure we aren't adding ourselves
    if user_id == friend_user_id:
        return

    # Check user isn't already a friend of ours
    if await glob.db.fetch(
        "SELECT id FROM users_relationships WHERE user1 = %s AND user2 = %s",
        [user_id, friend_user_id],
    ):
        return

    # Set new value
    await glob.db.execute(
        "INSERT INTO users_relationships (user1, user2) VALUES (%s, %s)",
        [user_id, friend_user_id],
    )


async def remove_friend(user_id: int, friend_user_id: int) -> None:
    """Delete a relationship between a given user and a friend."""
    await glob.db.execute(
        "DELETE FROM users_relationships WHERE user1 = %s AND user2 = %s",
        [user_id, friend_user_id],
    )


async def get_iso_country_code(user_id: int) -> str:
    """Get a user's ISO 3166-1 alpha-2 country code."""
    rec = await glob.db.fetch(
        "SELECT country FROM users WHERE id = %s",
        [user_id],
    )
    return rec["country"] if rec else "XX"  # type: ignore[no-any-return]


async def set_iso_country_code(user_id: int, iso_country_code: str) -> None:
    """Update a user's country code."""
    await glob.db.execute(
        "UPDATE users SET country = %s WHERE id = %s",
        [iso_country_code, user_id],
    )


async def associate_user_with_ip(user_id: int, ip_address: str) -> None:
    """
    Associate a user with a given ip address.

    This is used for multi-account detection.
    """
    await glob.db.execute(
        "INSERT INTO ip_user (userid, ip, occurencies) VALUES (%s, %s, 1) "
        "ON DUPLICATE KEY UPDATE occurencies = occurencies + 1",
        [user_id, ip_address],
    )


async def set_privileges(user_id: int, new_privileges: int) -> None:
    """Update a user's privileges."""
    await glob.db.execute(
        "UPDATE users SET privileges = %s WHERE id = %s",
        [new_privileges, user_id],
    )


def validate_hwid_set(hwid_set: list[str]) -> bool:
    """Validate that the mac addresses, unique id, and disk id are present."""
    return all(hwid_set[2:5])


async def associate_user_with_hwids_and_restrict_if_multiaccounting(
    user_id: int,
    # TODO: refactor hwid sets into an object across the codebase
    hwid_set: list[str],
    *,
    associate_with_account_activation: bool = False,
) -> None:
    """
    Associate a user with a given set of hardware identifiers.

    This function *may* restrict users who are determined to be engaging
    in multi-accounting.

    If `associate_with_account_activation` is True, set this hash as 'used for activation'.

    The hashset comes in the following form:
      - [0]: osu! version
      - [1]: plain mac addressed, separated by "."
      - [2]: mac addresses hash set
      - [3]: unique ID
      - [4]: disk ID
    """

    # Get username
    username = await get_username_from_id(user_id)

    # Run some HWID checks on that user if he is not restricted
    if not await is_restricted(user_id):
        # Get the list of banned or restricted users that have logged in from this or similar HWID hash set
        if hwid_set[2] == "b4ec3c4334a0249dae95c284ec5983df":
            # Running under wine, check by unique id
            logger.debug("Logging Linux/Mac hardware")
            banned = await glob.db.fetchAll(
                """
                SELECT users.id as userid, hw_user.occurencies, users.username
                FROM hw_user
                LEFT JOIN users ON users.id = hw_user.userid
                WHERE hw_user.userid != %(userid)s
                AND hw_user.unique_id = %(uid)s
                AND (users.privileges & 3 != 3)
                """,
                {
                    "userid": user_id,
                    "uid": hwid_set[3],
                },
            )
        else:
            # Running under windows, do all checks
            logger.debug("Logging Windows hardware")
            banned = await glob.db.fetchAll(
                """
                SELECT users.id as userid, hw_user.occurencies, users.username
                FROM hw_user
                LEFT JOIN users ON users.id = hw_user.userid
                WHERE hw_user.userid != %(userid)s
                AND hw_user.mac = %(mac)s
                AND hw_user.unique_id = %(uid)s
                AND hw_user.disk_id = %(diskid)s
                AND (users.privileges & 3 != 3)
                """,
                {
                    "userid": user_id,
                    "mac": hwid_set[2],
                    "uid": hwid_set[3],
                    "diskid": hwid_set[4],
                },
            )

        banned_ids = []
        for i in banned:
            if i["userid"] in banned_ids:
                continue

            # Get the total numbers of logins
            user_hwids_count_rec = await glob.db.fetch(
                """
                SELECT COUNT(*) AS count
                FROM hw_user
                WHERE userid = %s
                """,
                [user_id],
            )
            # and make sure it is valid
            if not user_hwids_count_rec:
                continue
            total = user_hwids_count_rec["count"]

            # Calculate 10% of total
            if i["occurencies"] >= (total * 10) / 100:
                # If the banned user has logged in more than 10% of the times from this user, restrict this user
                await restrict(user_id)
                await append_cm_notes(
                    user_id,
                    f'Logged in from HWID set used more than 10% from user {i["username"],} ({i["userid"]}), who is banned/restricted.',
                )
                await audit_logs.send_log_as_discord_webhook(
                    message=f'[{username}](https://akatsuki.gg/u/{user_id}) has been restricted because he has logged in from HWID set used more than 10% from banned/restricted user [{i["username"]}](https://akatsuki.gg/u/{i["userid"]}), **possible multiaccount**.',
                    discord_channel="ac_general",
                )
            banned_ids.append(i["userid"])

    # Update hash set occurencies
    await glob.db.execute(
        """
        INSERT INTO hw_user (id, userid, mac, unique_id, disk_id, occurencies)
        VALUES (NULL, %s, %s, %s, %s, 1)
        ON DUPLICATE KEY UPDATE occurencies = occurencies + 1
        """,
        [user_id, hwid_set[2], hwid_set[3], hwid_set[4]],
    )

    # Optionally, set this hash as 'used for activation'
    if associate_with_account_activation:
        await glob.db.execute(
            "UPDATE hw_user SET activated = 1 WHERE userid = %s AND mac = %s AND unique_id = %s AND disk_id = %s",
            [user_id, hwid_set[2], hwid_set[3], hwid_set[4]],
        )


async def mark_user_as_verified(user_id: int) -> None:
    """
    Remove the "pending verification" flag from a user,
    and set their basic user permissions.
    """
    await glob.db.execute(
        "UPDATE users SET privileges = privileges & %s WHERE id = %s",
        [~privileges.USER_PENDING_VERIFICATION, user_id],
    )


async def grant_user_default_privileges(user_id: int) -> None:
    """Grant a user the default publicly visible and normal privileges."""
    await glob.db.execute(
        "UPDATE users SET privileges = privileges | %s WHERE id = %s",
        [privileges.USER_PUBLIC | privileges.USER_NORMAL, user_id],
    )


WINE_STATIC_MAC_ADDRESS = "b4ec3c4334a0249dae95c284ec5983df"
WINE_STATIC_DISK_ID = "ffae06fb022871fe9beb58b005c5e21d"


def _hwid_set_running_under_wine(hwid_set: list[str]) -> bool:
    return hwid_set[2] == WINE_STATIC_MAC_ADDRESS or hwid_set[4] == WINE_STATIC_DISK_ID


async def find_user_ids_with_hwid_matches(
    user_id: int,
    hwid_set: list[str],
) -> list[int]:
    """\
    Find any users using the same set of hardware identifiers.

    In practice, we use this to determine users re-using the same hardware.

    This often a common sign of multi-accounting, which is against our ToS.
    """
    if _hwid_set_running_under_wine(hwid_set):
        # Running under wine, check only by uniqueid
        matching_records = await glob.db.fetchAll(
            """
            SELECT userid
            FROM hw_user
            WHERE unique_id = %(uid)s
            AND userid != %(userid)s
            AND activated = 1
            LIMIT 1
            """,
            {"uid": hwid_set[3], "userid": user_id},
        )
    else:
        # Running under windows, full check
        matching_records = await glob.db.fetchAll(
            """
            SELECT userid
            FROM hw_user
            WHERE mac = %(mac)s
            AND unique_id = %(uid)s
            AND disk_id = %(diskid)s
            AND userid != %(userid)s
            AND activated = 1
            LIMIT 1
            """,
            {
                "mac": hwid_set[2],
                "uid": hwid_set[3],
                "diskid": hwid_set[4],
                "userid": user_id,
            },
        )

    return [rec["userid"] for rec in matching_records]


async def authorize_login_and_activate_new_account(
    user_id: int,
    # TODO: refactor hwid sets into an object across the codebase
    hwid_set: list[str],
) -> bool:
    """
    Check for multi-accounts, authorize the login, activate the account (if new),
    and grant them default user privileges (publicity & regular access).
    """
    username = await get_username_from_id(user_id)

    user_ids_associated_with_device = await find_user_ids_with_hwid_matches(
        user_id,
        hwid_set,
    )

    if user_ids_associated_with_device:
        # There are other users associated with this set of hardware identifiers.
        # We'll consider this a multi-account; restrict original account; ban this account.

        # Fetch their original account's information
        original_user_id = user_ids_associated_with_device[0]
        original_username = await get_username_from_id(original_user_id)

        # Ban this user and append notes
        await ban(user_id)  # this removes the USER_PENDING_VERIFICATION flag too
        await append_cm_notes(
            user_id,
            f"{original_username}'s multiaccount ({original_user_id}), found HWID match while verifying account.",
        )
        await append_cm_notes(
            original_user_id,
            f"Has created multiaccount {username} ({user_id}).",
        )

        # Restrict the original
        await restrict(original_user_id)

        # Discord message
        await audit_logs.send_log_as_discord_webhook(
            message=f"[{original_username}](https://akatsuki.gg/u/{original_user_id}) has been restricted because they have created the multiaccount [{username}](https://akatsuki.gg/u/{user_id}). The multiaccount has been banned.",
            discord_channel="ac_general",
        )

        # Do not authorize login
        return False
    else:
        # No multiaccount matches found.
        # Verify the user and grant them default privileges.
        # TODO: only make db calls if they don't already have these.
        await mark_user_as_verified(user_id)
        await grant_user_default_privileges(user_id)

        # Authorize login
        return True


async def has_verified_with_any_hardware(user_id: int) -> bool:
    """Checks if a user has verified their account with any hardware."""
    return (
        await glob.db.fetch(
            "SELECT 1 FROM hw_user WHERE userid = %s AND activated = 1",
            [user_id],
        )
    ) is not None


async def get_absolute_donor_expiry_time(user_id: int) -> int:
    """Return a user's absolute donor expiry time."""
    data = await glob.db.fetch(
        "SELECT donor_expire FROM users WHERE id = %s",
        [user_id],
    )
    return data["donor_expire"] if data else 0  # type: ignore[no-any-return]


async def set_absolute_donor_expiry_time(user_id: int, donor_expire: int) -> None:
    """Sets a user's donor expire time"""
    await glob.db.execute(
        "UPDATE users SET donor_expire = %s WHERE id = %s",
        [donor_expire, user_id],
    )


async def add_user_badge(user_id: int, badge_id: int) -> None:
    """Adds a badge to a user"""
    await glob.db.execute(
        "INSERT INTO user_badges (user, badge) VALUES (%s, %s)",
        [user_id, badge_id],
    )


async def remove_user_badge(user_id: int, badge_id: int) -> None:
    """Removes specified badge from user"""
    await glob.db.execute(
        "DELETE FROM user_badges WHERE user = %s AND badge = %s",
        [user_id, badge_id],
    )


class InvalidUsernameError(Exception):
    pass


class UsernameAlreadyInUseError(Exception):
    pass


def get_safe_username(username: str) -> str:
    """
    Return username's "safe" username.

    We define "safe" as:
      1. all characters converted to lowercase.
      2. all spaces converted to underscores.
    """
    return username.lower().strip().replace(" ", "_")


async def change_username(user_id: int, new_username: str) -> None:
    """
    Update a user's username to a new value in the database.

    May raise either `InvalidUsernameError` or `UsernameAlreadyInUseError`.
    """
    # Make sure new username doesn't have mixed spaces and underscores
    if " " in new_username and "_" in new_username:
        raise InvalidUsernameError()

    old_username = await get_username_from_id(user_id)
    assert old_username is not None
    old_safe_username = get_safe_username(old_username)

    # this is done twice in username command dont worry about it
    # Get safe username
    new_safe_username = get_safe_username(new_username)

    # Make sure this username is not already in use
    new_username_owner_user_id = await get_id_from_safe_username(new_safe_username)
    if new_username_owner_user_id == user_id:
        if new_username == old_username:
            raise UsernameAlreadyInUseError()

        # pass on the case where the casing or spacing
        # on the new username is different from the old one
    elif new_username_owner_user_id is not None:
        raise UsernameAlreadyInUseError()

    # Change username
    await glob.db.execute(
        "UPDATE users SET username = %s, username_safe = %s WHERE id = %s",
        [new_username, new_safe_username, user_id],
    )

    # Empty redis username cache
    async with glob.redis.pipeline() as pipe:
        await pipe.delete(f"ripple:userid_cache:{old_safe_username}")
        await pipe.delete(f"ripple:change_username_pending:{user_id}")
        await pipe.execute()


async def remove_from_leaderboard(user_id: int) -> None:
    """
    Remove a user's listings from the global and country leaderboards

    Removes listings across all game modes.
    """
    country = (await get_iso_country_code(user_id)).lower()

    async with glob.redis.pipeline() as pipe:
        for board in ("leaderboard", "relaxboard"):
            for mode in ("std", "taiko", "ctb", "mania"):
                await pipe.zrem(f"ripple:{board}:{mode}", str(user_id))
                if country != "xx":
                    await pipe.zrem(f"ripple:{board}:{mode}:{country}", str(user_id))

        await pipe.execute()


async def remove_from_specified_leaderboard(
    user_id: int,
    mode: int,
    relax: int,
) -> None:
    country = (await get_iso_country_code(user_id)).lower()

    board = {
        0: "leaderboard",
        1: "relaxboard",
        2: "autoboard",
    }[relax]
    mode_str = {
        0: "std",
        1: "taiko",
        2: "ctb",
        3: "mania",
    }[mode]

    redis_board = f"ripple:{board}:{mode_str}"

    async with glob.redis.pipeline() as pipe:
        await pipe.zrem(redis_board, str(user_id))
        if country != "xx":
            await pipe.zrem(f"{redis_board}:{country}", str(user_id))

        await pipe.execute()


async def get_remaining_overwrite_wait(user_id: int) -> int:
    """
    Get the remaining time until a user may use !overwrite again.

    There is a forced 60s wait between overwrites (to mitigate DOS/load risk).
    """
    rec = await glob.db.fetch(
        "SELECT previous_overwrite FROM users WHERE id = %s",
        [user_id],
    )
    assert rec is not None
    return rec["previous_overwrite"]  # type: ignore[no-any-return]


async def remove_first_place(
    user_id: int,
    # Filter params
    akat_mode: int | None = None,
    game_mode: int | None = None,
) -> None:
    # Go through all of the users first place scores.
    # If we find a better play, transfer the #1 to them,
    # otherwise simply delete the #1 from the db.
    q = ["SELECT scoreid, beatmap_md5, mode, rx FROM scores_first WHERE userid = %s"]

    if akat_mode is not None:
        q.append(f"AND rx = {akat_mode}")
    if game_mode is not None:
        q.append(f"AND mode = {game_mode}")

    for score in await glob.db.fetchAll(" ".join(q), [user_id]):
        if score["rx"]:
            table = "scores_relax"
            sort = "pp"
        else:
            table = "scores"
            sort = "score"

        new = await glob.db.fetch(  # Get the 2nd top play.
            "SELECT s.id, s.userid FROM {t} s "
            "LEFT JOIN users u ON s.userid = u.id "
            "WHERE s.beatmap_md5 = %s AND s.play_mode = %s "
            "AND s.userid != %s AND s.completed = 3 AND u.privileges & 1 "
            "ORDER BY s.{s} DESC LIMIT 1".format(t=table, s=sort),
            [score["beatmap_md5"], score["mode"], user_id],
        )

        if new:  # Transfer the #1 to the old #2.
            await glob.db.execute(
                "UPDATE scores_first SET scoreid = %s, userid = %s "
                "WHERE scoreid = %s",
                [new["id"], new["userid"], score["scoreid"]],
            )
        else:  # There is no 2nd place, this was the only score.
            await glob.db.execute(
                "DELETE FROM scores_first WHERE scoreid = %s",
                [score["scoreid"]],
            )


async def recalculate_and_update_first_place_scores(user_id: int) -> None:
    """
    Perform a recalculation and DB/redis updating of all #1 scores for a user.

    This is typically used when a user is unrestricted, and we wish to
    give them back their #1 scores.

    This works for vanilla, relax and autopilot.
    """

    # The algorithm works as follows:
    #   - Go through all of the users plays, check if any are #1.
    #   - If they are, check if theres a score in scores_first.
    #   - If there is, overwrite that #1 with ours, otherwise
    #   - add the score to scores_first.

    for rx, table_name in enumerate(("scores", "scores_relax", "scores_ap")):
        order = "pp" if rx in (1, 2) else "score"
        for score in await glob.db.fetchAll(
            "SELECT s.id, s.{order} AS score_value, s.play_mode, "
            "s.beatmap_md5, b.ranked FROM {t} s "
            "LEFT JOIN beatmaps b USING(beatmap_md5) "
            "WHERE s.userid = %s AND s.completed = 3 "
            "AND s.score > 0 AND b.ranked > 1".format(order=order, t=table_name),
            [user_id],
        ):
            # Vanilla always uses score to determine #1s.

            # Get the current first place.
            existing_first_place = await glob.db.fetch(
                f"""
                SELECT scores_first.scoreid, scores_first.userid, scores.{order} AS score_value
                FROM scores_first
                JOIN {table_name} AS scores ON scores.id = scores_first.scoreid
                WHERE beatmap_md5 = %s
                AND scores_first.mode = %s
                AND scores_first.rx = %s
                """,
                [score["beatmap_md5"], score["play_mode"], rx],
            )

            # Check if our score is better than the current #1.
            # If it is, then add/update scores_first.
            if (
                not existing_first_place
                or score["score_value"] > existing_first_place["score_value"]
            ):
                await glob.db.execute(
                    "REPLACE INTO scores_first VALUES (%s, %s, %s, %s, %s)",
                    [
                        score["beatmap_md5"],
                        score["play_mode"],
                        rx,
                        score["id"],
                        user_id,
                    ],
                )


def get_profile_url(user_id: int) -> str:
    return f"https://akatsuki.gg/u/{user_id}"


async def get_profile_url_osu_chat_embed(
    user_id: int,
    *,
    include_clan: bool = False,
) -> str | None:
    profile_embed = (
        f"({await get_username_from_id(user_id)})[{get_profile_url(user_id)}]"
    )

    # get their clan id & tag for embed
    if include_clan:
        res = await glob.db.fetch(
            "SELECT c.id, c.tag FROM clans c "
            "LEFT JOIN users u ON c.id = u.clan_id "
            "WHERE u.id = %s",
            [user_id],
        )

        if res:
            clan_embed = "[[https://akatsuki.gg/c/{id} {tag}]]".format(**res)
            return f"{clan_embed} {profile_embed}"

    return profile_embed
