from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime as dt
from datetime import timedelta as td

from amplitude.event import BaseEvent
from amplitude.event import EventOptions
from amplitude.event import Identify

from common import generalUtils
from common.constants import privileges
from common.log import audit_logs
from common.log import logger
from common.ripple import user_utils
from common.web.requestsManager import AsyncRequestHandler
from constants import CHATBOT_USER_ID
from constants import exceptions
from constants import serverPackets
from helpers import chatHelper as chat
from helpers import locationHelper
from objects import channelList
from objects import glob
from objects import osuToken
from objects import stream
from objects import stream_messages
from objects import tokenList
from objects import verifiedCache

osu_ver_regex = re.compile(
    r"^b(?P<ver>\d{8})(?:\.(?P<subver>\d))?"
    r"(?P<stream>beta|cuttingedge|dev|tourney)?$",
)


async def handle(web_handler: AsyncRequestHandler) -> tuple[str, bytes]:  # token, data
    # Data to return
    userToken = None
    responseTokenString = "ayy"
    responseData = bytearray()

    login_timestamp = time.time()

    # Get client ip of the incoming request
    request_ip_address = web_handler.getRequestIP()
    if not request_ip_address:
        logger.warning("Failed to resolve a request IP for a login request")
        return responseTokenString, responseData

    # Split POST body so we can get username/password/hardware data
    loginData = web_handler.request.body.decode()[:-1].split("\n")

    userID: int | None = None
    username: str | None = None
    osuVersionStr: str | None = None
    restricted: bool | None = None

    try:
        # Make sure loginData is valid
        if len(loginData) < 3:
            raise exceptions.invalidArgumentsException()

        # Get HWID, MAC address and more
        # Structure (new line = "|", already split)
        # [0] osu! version
        # [1] plain mac addresses, separated by "."
        # [2] mac addresses hash set
        # [3] unique ID
        # [4] disk ID
        splitData = loginData[2].split("|")
        osuVersionStr = splitData[0]
        utc_offset = int(splitData[1])
        if len(clientData := splitData[3].split(":")[:5]) < 4:
            raise exceptions.forceUpdateException()
        block_non_friends_dm = splitData[4] == "1"

        # Try to get the ID from username
        username = loginData[0]
        userID = await user_utils.get_id_from_username(username)

        if not userID:
            # Invalid username
            raise exceptions.loginFailedException()
        elif userID == CHATBOT_USER_ID:
            raise exceptions.invalidArgumentsException()

        if not await user_utils.authenticate(userID, loginData[1]):
            # Invalid password
            raise exceptions.loginFailedException()

        # Make sure we are not banned or locked
        priv = await user_utils.get_privileges(userID)
        pending_verification = priv & privileges.USER_PENDING_VERIFICATION != 0

        if not pending_verification:
            if not priv & (privileges.USER_PUBLIC | privileges.USER_NORMAL):
                raise exceptions.loginBannedException()
            if (
                priv & privileges.USER_PUBLIC != 0
                and priv & privileges.USER_NORMAL == 0
            ):
                raise exceptions.loginLockedException()

        restricted = priv & privileges.USER_PUBLIC == 0

        if v_argstr in web_handler.request.arguments or osuVersionStr == v_argverstr:
            raise exceptions.haxException()

        if not (rgx := osu_ver_regex.match(osuVersionStr)):
            raise exceptions.loginFailedException()

        osuVersion = dt(
            year=int(rgx["ver"][0:4]),
            month=int(rgx["ver"][4:6]),
            day=int(rgx["ver"][6:8]),
        )

        # disallow clients older than 1 year
        if osuVersion < (dt.now() - td(365)):
            logger.warning(
                "Denied login from client too old",
                extra={"version": osuVersionStr},
            )
            raise exceptions.haxException()

        """ No login errors! """

        # Verify this user (if pending activation)
        firstLogin = False
        shouldBan = False
        if pending_verification or not await user_utils.has_verified_with_any_hardware(
            userID,
        ):
            if await user_utils.authorize_login_and_activate_new_account(
                userID,
                clientData,
            ):
                # Valid account
                logger.info(
                    "User verified their account",
                    extra={"user_id": userID, "username": username},
                )
                await verifiedCache.set(userID, True)
                firstLogin = True
            else:
                # Multiaccount detected
                logger.warning(
                    "User tried to create another account",
                    extra={"user_id": userID, "username": username},
                )
                await verifiedCache.set(userID, False)
                shouldBan = True

        if not user_utils.validate_hwid_set(clientData):
            await audit_logs.send_log_as_discord_webhook(
                message=f"Invalid hash set ({clientData}) for user [{username}](https://akatsuki.gg/u/{userID}) in HWID check",
                discord_channel="ac_confidential",
            )
            raise exceptions.haxException()

        # Save HWID in db for multiaccount detection, and restrict them if
        # they are determined to be engaging in multi-accounting.
        await user_utils.associate_user_with_hwids_and_restrict_if_multiaccounting(
            userID,
            clientData,
            associate_with_account_activation=firstLogin,
        )

        # Log user IP
        await user_utils.associate_user_with_ip(userID, request_ip_address)

        if shouldBan:
            await user_utils.ban(userID)
            raise exceptions.loginBannedException()

        # Delete old tokens for that user and generate a new one
        isTournament = rgx["stream"] == "tourney"

        if clientData[4] == "dcfcd07e645d245babe887e5e2daa016":
            # NOTE: this is the result of `md5(md5("0"))`.
            # The osu! client will send this sometimes because WMI
            # may return a "0" as the disk serial number if a hardware
            # manufacturer has not set one.
            # (disk signature is optional but serial number is required)
            amplitude_device_id = None
        else:
            amplitude_device_id = hashlib.sha1(clientData[4].encode()).hexdigest()

        if not isTournament:
            await tokenList.deleteOldTokens(userID)

        userToken = await tokenList.addToken(
            userID,
            ip=request_ip_address,
            utc_offset=utc_offset,
            tournament=isTournament,
            block_non_friends_dm=block_non_friends_dm,
            amplitude_device_id=amplitude_device_id,
        )
        username = userToken["username"]  # trust the one from the db

        responseTokenString = userToken["token_id"]

        # Console output
        logger.info(
            "User logged in",
            extra={
                "user_id": userID,
                "username": username,
                "online_users": await osuToken.get_online_players_count(),
            },
        )

        await osuToken.notifyUserOfRestrictionStatusChange(userToken["token_id"])

        """ osu!Akatuki account freezing. """

        # Get the user's `frozen` status from the DB
        # For a normal user, this will return 0.
        # For a frozen user, this will return a unix timestamp (the date of their pending restriction).
        freeze_timestamp = await user_utils.get_freeze_restriction_date(userID)
        if freeze_timestamp:
            # The user has an active freeze.
            # Next, we must determine if it has expired.

            if freeze_timestamp > login_timestamp:
                # The freeze has _not_ expired. Warn the user about it.
                chatbot_token = await osuToken.get_token_by_user_id(CHATBOT_USER_ID)
                assert chatbot_token is not None

                await chat.send_message(
                    sender_token_id=chatbot_token["token_id"],
                    recipient_name=username,
                    message="\n".join(
                        [
                            "Your account has been frozen by Akatsuki staff.",
                            "This is not a restriction, but will lead to one if ignored.",
                            "You are required to submit a liveplay using the (specified criteria)[https://bit.ly/liveplay-criteria]",
                            "If you have any questions or are ready to liveplay, please open a ticket on our (Discord)[https://akatsuki.gg/discord].",
                            f"Time left until account restriction: {td(seconds=freeze_timestamp - login_timestamp)}.",
                        ],
                    ),
                )

            else:
                # The user's freeze has expired; restrict them.
                await user_utils.restrict(userID)

                maybe_token = await osuToken.update_token(
                    userToken["token_id"],
                    privileges=userToken["privileges"] & ~privileges.USER_PUBLIC,
                )
                assert maybe_token is not None
                userToken = maybe_token

                await user_utils.unfreeze(
                    userID,
                    should_log_to_cm_notes_and_discord=False,
                )

                await osuToken.enqueue(
                    userToken["token_id"],
                    serverPackets.notification(
                        "\n\n".join(
                            [
                                "Your account has been automatically restricted due to an account freeze being left unhandled for over 7 days.",
                                "You are still welcome to liveplay, although your account will remain in restricted mode unless this is handled.",
                            ],
                        ),
                    ),
                )
                await audit_logs.send_log(
                    userID,
                    "has been automatically restricted due to a pending freeze.",
                )
                await audit_logs.send_log_as_discord_webhook(
                    message=f"[{username}](https://akatsuki.gg/u/{userID}) has been automatically restricted due to a pending freeze.",
                    discord_channel="ac_general",
                )

        # Handle donor expiry, or notify the user if it's upcoming.
        if userToken["privileges"] & privileges.USER_DONOR:
            donor_expiry_timestamp = await user_utils.get_absolute_donor_expiry_time(
                userID,
            )
            premium = userToken["privileges"] & privileges.USER_PREMIUM
            donor_role_name = "premium" if premium else "supporter"

            if login_timestamp >= donor_expiry_timestamp:
                # Revoke the user's supporter/premium status
                await user_utils.set_privileges(
                    userID,
                    userToken["privileges"] - privileges.USER_DONOR
                    | (privileges.USER_PREMIUM if premium else 0),
                )

                # Delete any supporter/premium badges from the user
                await glob.db.execute(
                    """\
                    DELETE FROM user_badges
                    WHERE user = %s
                    AND badge IN (59, 36)
                    """,
                    [userID],
                )

                # Remove their custom badge privileges
                await glob.db.execute(
                    """\
                    UPDATE users
                    SET can_custom_badge = 0, show_custom_badge = 0
                    WHERE id = %s
                    """,
                    [userID],
                )

                await audit_logs.send_log(
                    userID,
                    f"{donor_role_name} subscription expired.",
                )
                await audit_logs.send_log_as_discord_webhook(
                    message=f"[{username}](https://akatsuki.gg/u/{userID})'s {donor_role_name} subscription has expired.",
                    discord_channel="ac_confidential",
                )

                await osuToken.enqueue(
                    userToken["token_id"],
                    serverPackets.notification(
                        "\n\n".join(
                            [
                                f"Your {donor_role_name} tag has expired.",
                                "Whether you continue to support us or not, we'd like to thank you "
                                "to the moon and back for your support so far - it really means everything to us.",
                                "- cmyui, and the Akatsuki Team",
                            ],
                        ),
                    ),
                )

            elif donor_expiry_timestamp - login_timestamp <= 86400 * 7:
                # There's under 7 days left in the donor tag;
                # Let the user know the expiry time is drawing near
                expireIn = generalUtils.secondsToReadable(
                    int(donor_expiry_timestamp - login_timestamp),
                )
                await osuToken.enqueue(
                    userToken["token_id"],
                    serverPackets.notification(
                        f"Your {donor_role_name} tag expires in {expireIn}.",
                    ),
                )

        # Set silence end UNIX time in token
        maybe_token = await osuToken.update_token(
            userToken["token_id"],
            silence_end_time=await user_utils.get_absolute_silence_end(userID),
        )
        assert maybe_token is not None
        userToken = maybe_token

        # Get only silence remaining seconds
        silenceSeconds = await osuToken.getSilenceSecondsLeft(userToken["token_id"])

        # Send login notification before maintenance message
        if glob.banchoConf.config["loginNotification"]:
            await osuToken.enqueue(
                userToken["token_id"],
                serverPackets.notification(glob.banchoConf.config["loginNotification"]),
            )

        # Maintenance check
        if glob.banchoConf.config["banchoMaintenance"]:
            if not osuToken.is_staff(userToken["privileges"]):
                # We are not mod/admin, delete token, send notification and logout
                await tokenList.deleteToken(responseTokenString)
                raise exceptions.banchoMaintenanceException()
            else:
                # We are mod/admin, send warning notification and continue
                await osuToken.enqueue(
                    userToken["token_id"],
                    serverPackets.notification(
                        "Akatsuki is currently in maintenance mode. Only admins have full access to the server.\n"
                        "Type '!system maintenance off' in chat to turn off maintenance mode.",
                    ),
                )

        # Send all needed login packets
        await osuToken.enqueue(userToken["token_id"], serverPackets.protocolVersion(19))
        await osuToken.enqueue(userToken["token_id"], serverPackets.userID(userID))
        await osuToken.enqueue(
            userToken["token_id"],
            serverPackets.silenceEndTime(silenceSeconds),
        )
        await osuToken.enqueue(
            userToken["token_id"],
            serverPackets.userSupporterGMT(
                is_supporter=True,
                is_gmt=osuToken.is_staff(userToken["privileges"]),
                is_tourney_staff=(
                    userToken["privileges"] & privileges.USER_TOURNAMENT_STAFF > 0
                ),
            ),
        )
        await osuToken.enqueue(
            userToken["token_id"],
            await serverPackets.userPanel(userID, force=True),
        )
        await osuToken.enqueue(
            userToken["token_id"],
            await serverPackets.userStats(userID, force=True),
        )

        # Default opened channels.
        await chat.join_channel(token_id=userToken["token_id"], channel_name="#osu")
        await chat.join_channel(
            token_id=userToken["token_id"],
            channel_name="#announce",
        )

        # Join role-related channels.
        if userToken["privileges"] & privileges.ADMIN_CAKER:
            await chat.join_channel(
                token_id=userToken["token_id"],
                channel_name="#devlog",
            )
        if osuToken.is_staff(userToken["privileges"]):
            await chat.join_channel(
                token_id=userToken["token_id"],
                channel_name="#staff",
            )
        if userToken["privileges"] & privileges.USER_PREMIUM:
            await chat.join_channel(
                token_id=userToken["token_id"],
                channel_name="#premium",
            )
        if userToken["privileges"] & privileges.USER_DONOR:
            await chat.join_channel(
                token_id=userToken["token_id"],
                channel_name="#supporter",
            )

        # Output channels info
        for channel in await channelList.getChannels():
            if channel["public_read"] and not channel["instance"]:
                client_count = await stream.get_client_count(f"chat/{channel['name']}")
                packet_data = serverPackets.channelInfo(
                    channel["name"],
                    channel["description"],
                    client_count,
                )
                await osuToken.enqueue(userToken["token_id"], packet_data)

        # Channel info end.
        await osuToken.enqueue(userToken["token_id"], serverPackets.channelInfoEnd)

        # Send friends list
        friends_list = await user_utils.get_friend_user_ids(userID)
        if friends_list:
            await osuToken.enqueue(
                userToken["token_id"],
                serverPackets.friendList(userID, friends_list),
            )

        # Send main menu icon
        if glob.banchoConf.config["menuIcon"]:
            await osuToken.enqueue(
                userToken["token_id"],
                serverPackets.mainMenuIcon(glob.banchoConf.config["menuIcon"]),
            )

        # Send online users' panels
        for token in await osuToken.get_tokens():
            if not osuToken.is_restricted(token["privileges"]):
                await osuToken.enqueue(
                    userToken["token_id"],
                    await serverPackets.userPanel(token["user_id"]),
                )

        # Get location and country from client ip address
        geolocation = await locationHelper.resolve_ip_geolocation(request_ip_address)

        # Set location and country
        await osuToken.setLocation(
            userToken["token_id"],
            geolocation["latitude"],
            geolocation["longitude"],
        )
        maybe_token = await osuToken.update_token(
            userToken["token_id"],
            country=geolocation["osu_country_code"],
        )
        assert maybe_token is not None
        userToken = maybe_token

        # Set country in db if user has no country (first bancho login)
        if await user_utils.get_iso_country_code(userID) == "XX":
            await user_utils.set_iso_country_code(
                userID,
                geolocation["iso_country_code"],
            )

        # Send to everyone our userpanel if we are not restricted or tournament
        if not osuToken.is_restricted(userToken["privileges"]):
            await stream_messages.broadcast_data(
                "main",
                await serverPackets.userPanel(userID),
            )

        if glob.amplitude is not None:
            glob.amplitude.track(
                BaseEvent(
                    event_type="osu_login",
                    user_id=str(userID),
                    device_id=userToken["amplitude_device_id"],
                    event_properties={
                        "username": userToken["username"],
                        "privileges": userToken["privileges"],
                        "login_time": userToken["login_time"],
                        "source": "bancho-service",
                    },
                    location_lat=geolocation["latitude"],
                    location_lng=geolocation["longitude"],
                    ip=request_ip_address,
                    country=geolocation["iso_country_code"],
                ),
            )

        if firstLogin:
            if glob.amplitude is not None:
                glob.amplitude.track(
                    BaseEvent(
                        event_type="osu_verification",
                        user_id=str(userID),
                        device_id=userToken["amplitude_device_id"],
                        event_properties={
                            "username": userToken["username"],
                            "privileges": userToken["privileges"],
                            "login_time": userToken["login_time"],
                            "source": "bancho-service",
                        },
                        location_lat=geolocation["latitude"],
                        location_lng=geolocation["longitude"],
                        ip=request_ip_address,
                        country=geolocation["iso_country_code"],
                    ),
                )

        if glob.amplitude is not None:
            identify_obj = Identify()
            identify_obj.set("username", userToken["username"])
            identify_obj.set("location_lat", geolocation["latitude"])
            identify_obj.set("location_lng", geolocation["longitude"])
            identify_obj.set("ip", request_ip_address)
            identify_obj.set("country", geolocation["iso_country_code"])
            glob.amplitude.identify(
                identify_obj,
                EventOptions(
                    user_id=str(userID),
                    device_id=userToken["amplitude_device_id"],
                ),
            )

        # Set reponse data to right value and reset our queue
        queued_token_data = await stream_messages.read_all_pending_data(
            userToken["token_id"],
        )
        responseData = bytearray(queued_token_data)
    except exceptions.loginFailedException:
        # Login failed error packet
        # (we don't use enqueue because we don't have a token since login has failed)
        responseData += serverPackets.loginFailed
        responseData += serverPackets.notification(
            "Akatsuki: You have entered an incorrect username or password. Please check your credentials and try again!",
        )
    except exceptions.invalidArgumentsException:
        # Invalid POST data
        # (we don't use enqueue because we don't have a token since login has failed)
        responseData += serverPackets.loginFailed
        responseData += serverPackets.notification(
            "Akatsuki: Something went wrong during your login attempt... Please try again!",
        )
    except exceptions.loginBannedException:
        # Login banned error packet
        responseData += serverPackets.loginBanned
    except exceptions.loginLockedException:
        # Login banned error packet
        responseData += serverPackets.loginLocked
    except exceptions.banchoMaintenanceException:
        # Bancho is in maintenance mode
        if userToken:
            queued_token_data = await stream_messages.read_all_pending_data(
                userToken["token_id"],
            )
            responseData = bytearray(queued_token_data)
        else:
            responseData.clear()
        responseData += serverPackets.notification(
            "Akatsuki is currently in maintenance mode. Please try to login again later.",
        )
        responseData += serverPackets.loginFailed
    # except exceptions.need2FAException:
    #    # User tried to log in from unknown IP
    #    responseData += serverPackets.needVerification
    except exceptions.haxException:
        # Using old client, we don't have client data, force update (we don't use enqueue because we don't have a token since login has failed)
        responseData += serverPackets.loginFailed
        responseData += serverPackets.notification(
            "\n\n".join(
                [
                    "Hey!",
                    "The osu! client you're trying to use is out of date.",
                    "Custom/out of date osu! clients are not permitted on Akatsuki.",
                    "Please relogin using the current osu! client - no fallback, sorry!",
                    "You will need to switch to Bancho in order to update your client.",
                ],
            ),
        )

        if (
            userID
            and username
            and not restricted
            and (
                v_argstr in web_handler.request.arguments
                or osuVersionStr == v_argverstr
            )
        ):
            await audit_logs.send_log_as_discord_webhook(
                message=f"**[{username}](https://akatsuki.gg/u/{userID})** has attempted to login with the {v_argstr} client.",
                discord_channel="ac_general",
            )
    except:
        logger.exception("An unhandled exception occurred during login")
    finally:
        # Console and discord log
        if len(loginData) < 3:
            logger.warning(
                "Invalid bancho login request",
                extra={
                    "reason": "insufficient_post_data",
                    "ip": request_ip_address,
                },
            )

            await audit_logs.send_log_as_discord_webhook(
                message=f"Invalid bancho login request from **{request_ip_address}** (insufficient POST data)",
                discord_channel="ac_confidential",
            )

        # Return token string and data
        return responseTokenString, bytes(responseData)


v_argstr = bytes([97, 105, 110, 117]).decode()
v_argverstr = bytes([48, 65, 105, 110, 117]).decode()
