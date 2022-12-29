""" Contains functions used to write specific server packets to byte streams """
from __future__ import annotations

from common.constants import privileges
from common.ripple import userUtils
from constants import dataTypes
from constants import packetIDs
from constants import userRanks
from helpers import packetHelper
from objects import glob, stream


def notification(message: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_notification,
        ((message, dataTypes.STRING),),
    )


""" Login errors packets """
loginFailed = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-1, dataTypes.SINT32),),
)

loginBanned = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-1, dataTypes.SINT32),),
) + notification(
    "You are banned. "
    "The earliest we accept appeals is 2 months after your "
    "most recent offense, and really only care for the truth.",
)

loginLocked = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-1, dataTypes.SINT32),),
) + notification(
    "Your account is locked. You can't log in, but your "
    "profile and scores are still visible from the website. "
    "The earliest we accept appeals is 2 months after your "
    "most recent offense, and really only care for the truth.",
)

forceUpdate = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-2, dataTypes.SINT32),),
)

loginError = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-5, dataTypes.SINT32),),
)
needSupporter = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-6, dataTypes.SINT32),),
)
needVerification = packetHelper.buildPacket(
    packetIDs.server_userID,
    ((-8, dataTypes.SINT32),),
)


""" Login packets """


def userID(uid: int) -> bytes:
    return packetHelper.buildPacket(packetIDs.server_userID, ((uid, dataTypes.SINT32),))


def silenceEndTime(seconds: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_silenceEnd,
        ((seconds, dataTypes.UINT32),),
    )


def protocolVersion(version: int = 19) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_protocolVersion,
        ((version, dataTypes.UINT32),),
    )


def mainMenuIcon(icon: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_mainMenuIcon,
        ((icon, dataTypes.STRING),),
    )


def userSupporterGMT(supporter: bool, GMT: bool, tournamentStaff: bool) -> bytes:
    result = 1

    if supporter:
        result |= userRanks.SUPPORTER

    if GMT:
        result |= userRanks.BAT

    if tournamentStaff:
        result |= userRanks.TOURNAMENT_STAFF

    return packetHelper.buildPacket(
        packetIDs.server_supporterGMT,
        ((result, dataTypes.UINT32),),
    )


def friendList(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_friendsList,
        ((userUtils.getFriendList(userID), dataTypes.INT_LIST),),
    )


def onlineUsers() -> bytes:
    userIDs = []

    # Create list with all connected (and not restricted) users
    for value in glob.tokens.tokens.values():
        if not value.restricted:
            userIDs.append(value.userID)

    return packetHelper.buildPacket(
        packetIDs.server_userPresenceBundle,
        ((userIDs, dataTypes.INT_LIST),),
    )


""" Users packets """


def userLogout(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_userLogout,
        ((userID, dataTypes.SINT32), (0, dataTypes.BYTE)),
    )


BOT_PRESENCE = (
    b"S\x00\x00\x19\x00\x00\x00\xe7"
    b"\x03\x00\x00\x0b\x04Aika\x18"
    b"\x00\x10\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00"
)


def userPanel(userID: int, force: bool = False) -> bytes:
    if userID == 999:
        return BOT_PRESENCE

    # Connected and restricted check
    userToken = glob.tokens.getTokenFromUserID(userID)
    if not userToken or ((userToken.restricted) and not force):
        return b""

    # Get user data
    username = userToken.username
    timezone = 24 + userToken.timeOffset
    country = userToken.country
    gameRank = userToken.gameRank
    latitude, longitude = userToken.location

    dev_groups = []  # awful but better than like 5 sql queries
    if "developer" in glob.groupPrivileges:
        dev_groups.append(glob.groupPrivileges["developer"])
    if "head developer" in glob.groupPrivileges:
        dev_groups.append(glob.groupPrivileges["head developer"])

    # Get username color according to rank
    # Only admins and normal users are currently supported
    userRank = 0
    if userToken.privileges in dev_groups:
        userRank |= userRanks.ADMIN  # Developers - darker blue
    elif userToken.privileges & privileges.ADMIN_MANAGE_PRIVILEGES:
        userRank |= userRanks.PEPPY  # Administrators - lighter blue
    elif userToken.privileges & privileges.ADMIN_CHAT_MOD:
        userRank |= userRanks.MOD  # Community Managers - orange red
    elif userToken.privileges & privileges.USER_DONOR:
        userRank |= userRanks.SUPPORTER  # Supporter & premium - darker yellow
    else:
        userRank |= userRanks.NORMAL  # Regular - lighter yellow

    return packetHelper.buildPacket(
        packetIDs.server_userPanel,
        (
            (userID, dataTypes.SINT32),
            (username, dataTypes.STRING),
            (timezone, dataTypes.BYTE),
            (country, dataTypes.BYTE),
            (userRank, dataTypes.BYTE),
            (longitude, dataTypes.FFLOAT),
            (latitude, dataTypes.FFLOAT),
            (gameRank, dataTypes.UINT32),
        ),
    )


BOT_STATS = (
    b"\x0b\x00\x00.\x00\x00\x00\xe7\x03\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x17\xb7\xd1\xb8\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00"
)


def userStats(userID: int, force: bool = False) -> bytes:
    if userID == 999:
        return BOT_STATS

    # Get userID's token from tokens list
    userToken = glob.tokens.getTokenFromUserID(userID)
    if userToken is None:
        return b""

    if not force:
        if userToken.restricted or userToken.irc or userToken.tournament:
            return b""

    # If our PP is over the osu client's cap (32768), we simply send
    # our pp value as ranked score instead, and send pp as 0.
    # The rank will not be affected as it is calculated
    # server side rather than on the client.
    return packetHelper.buildPacket(
        packetIDs.server_userStats,
        (
            (userID, dataTypes.UINT32),
            (userToken.actionID, dataTypes.BYTE),
            (userToken.actionText, dataTypes.STRING),
            (userToken.actionMd5, dataTypes.STRING),
            (userToken.actionMods, dataTypes.SINT32),
            (userToken.gameMode, dataTypes.BYTE),
            (userToken.beatmapID, dataTypes.SINT32),
            (
                userToken.rankedScore if userToken.pp < 0x8000 else userToken.pp,
                dataTypes.UINT64,
            ),
            (userToken.accuracy, dataTypes.FFLOAT),
            (userToken.playcount, dataTypes.UINT32),
            (userToken.totalScore, dataTypes.UINT64),
            (userToken.gameRank, dataTypes.UINT32),
            (userToken.pp if userToken.pp < 0x8000 else 0, dataTypes.UINT16),
        ),
    )


""" Chat packets """


def sendMessage(fro: str, to: str, message: str, fro_id: int = 0) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_sendMessage,
        (
            (fro, dataTypes.STRING),
            (message, dataTypes.STRING),
            (to, dataTypes.STRING),
            (fro_id or userUtils.getID(fro), dataTypes.SINT32),
        ),
    )


def targetBlockingDMs(to: str, fro: str, fro_id: int = 0) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_targetBlockingNonFriendsDM,
        (
            (fro, dataTypes.STRING),
            ("", dataTypes.STRING),
            (to, dataTypes.STRING),
            (fro_id or userUtils.getID(fro), dataTypes.SINT32),
        ),
    )


def targetSilenced(to: str, fro: str, fro_id: int = 0) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_targetSilenced,
        (
            (fro, dataTypes.STRING),
            ("", dataTypes.STRING),
            (to, dataTypes.STRING),
            (fro_id or userUtils.getID(fro), dataTypes.SINT32),
        ),
    )


def channelJoinSuccess(chan: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_channelJoinSuccess,
        ((chan, dataTypes.STRING),),
    )


def channelInfo(chan: str) -> bytes:
    if chan not in glob.channels.channels:
        return b""

    channel = glob.channels.channels[chan]
    key = f"chat/{chan}"
    client_count = stream.getClientCount(key)

    return packetHelper.buildPacket(
        packetIDs.server_channelInfo,
        (
            (channel.name, dataTypes.STRING),
            (channel.description, dataTypes.STRING),
            (client_count, dataTypes.UINT16),
        ),
    )


channelInfoEnd = packetHelper.buildPacket(
    packetIDs.server_channelInfoEnd,
    ((0, dataTypes.UINT32),),
)


def channelKicked(chan: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_channelKicked,
        ((chan, dataTypes.STRING),),
    )


def userSilenced(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_userSilenced,
        ((userID, dataTypes.UINT32),),
    )


""" Spectator packets """


def addSpectator(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_spectatorJoined,
        ((userID, dataTypes.SINT32),),
    )


def removeSpectator(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_spectatorLeft,
        ((userID, dataTypes.SINT32),),
    )


def spectatorFrames(data: bytes) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_spectateFrames,
        ((data, dataTypes.BBYTES),),
    )


def noSongSpectator(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_spectatorCantSpectate,
        ((userID, dataTypes.SINT32),),
    )


def fellowSpectatorJoined(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_fellowSpectatorJoined,
        ((userID, dataTypes.SINT32),),
    )


def fellowSpectatorLeft(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_fellowSpectatorLeft,
        ((userID, dataTypes.SINT32),),
    )


""" Multiplayer Packets """


def createMatch(matchID: int) -> bytes:
    # Make sure the match exists
    if matchID not in glob.matches.matches:
        return b""

    # Get match binary data and build packet
    match = glob.matches.matches[matchID]
    matchData = match.getMatchData(censored=True)
    return packetHelper.buildPacket(packetIDs.server_newMatch, matchData)


# TODO: Add match object argument to save some CPU
def updateMatch(matchID: int, censored: bool = False) -> bytes:
    # Make sure the match exists
    if matchID not in glob.matches.matches:
        return b""

    # Get match binary data and build packet
    match = glob.matches.matches[matchID]
    return packetHelper.buildPacket(
        packetIDs.server_updateMatch,
        match.getMatchData(censored=censored),
    )


def matchStart(matchID: int) -> bytes:
    # Make sure the match exists
    if matchID not in glob.matches.matches:
        return b""

    # Get match binary data and build packet
    match = glob.matches.matches[matchID]
    return packetHelper.buildPacket(packetIDs.server_matchStart, match.getMatchData())


def disposeMatch(matchID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_disposeMatch,
        ((matchID, dataTypes.UINT32),),
    )


def matchJoinSuccess(matchID: int) -> bytes:
    # Make sure the match exists
    if matchID not in glob.matches.matches:
        return b""

    # Get match binary data and build packet
    match = glob.matches.matches[matchID]
    return packetHelper.buildPacket(
        packetIDs.server_matchJoinSuccess,
        match.getMatchData(),
    )


matchJoinFail = packetHelper.buildPacket(packetIDs.server_matchJoinFail)


def changeMatchPassword(newPassword: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_matchChangePassword,
        ((newPassword, dataTypes.STRING),),
    )


allPlayersLoaded = packetHelper.buildPacket(packetIDs.server_matchAllPlayersLoaded)


def playerSkipped(userID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_matchPlayerSkipped,
        ((userID, dataTypes.SINT32),),
    )


allPlayersSkipped = packetHelper.buildPacket(packetIDs.server_matchSkip)


def matchFrames(slotID: int, data: bytes) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_matchScoreUpdate,
        (
            (data[7:11], dataTypes.BBYTES),
            (slotID, dataTypes.BYTE),
            (data[12:], dataTypes.BBYTES),
        ),
    )


matchComplete = packetHelper.buildPacket(packetIDs.server_matchComplete)


def playerFailed(slotID: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_matchPlayerFailed,
        ((slotID, dataTypes.UINT32),),
    )


matchTransferHost = packetHelper.buildPacket(packetIDs.server_matchTransferHost)
matchAbort = packetHelper.buildPacket(packetIDs.server_matchAbort)


def switchServer(address: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_switchServer,
        ((address, dataTypes.STRING),),
    )


""" Other packets """


def banchoRestart(msUntilReconnection: int) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_restart,
        ((msUntilReconnection, dataTypes.UINT32),),
    )


def rtx(message: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_sendRTX,
        ((message, dataTypes.STRING),),
    )


def invalidChatMessage(username: str) -> bytes:
    return packetHelper.buildPacket(
        packetIDs.server_sendMessage,
        (
            ("", dataTypes.STRING),
            ("", dataTypes.STRING),
            (username, dataTypes.STRING),
            (999, dataTypes.SINT32),
        ),
    )


popChat = packetHelper.buildPacket(packetIDs.server_popChat)
