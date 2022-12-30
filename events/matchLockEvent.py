from __future__ import annotations

from constants import clientPackets
from objects import match
from objects.osuToken import token

from redlock import RedLock


def handle(userToken: token, rawPacketData: bytes):
    # Get packet data
    packetData = clientPackets.lockSlot(rawPacketData)

    # Make sure the match exists
    multiplayer_match = match.get_match(userToken.matchID)
    if multiplayer_match is None:
        return

    with RedLock(
        f"{match.make_key(userToken.matchID)}:lock",
        retry_delay=50,
        retry_times=20,
    ):
        # Host check
        if userToken.userID != multiplayer_match["host_user_id"]:
            return

        # Make sure we aren't locking our slot
        ourSlot = match.getUserSlotID(multiplayer_match["match_id"], userToken.userID)
        if packetData["slotID"] == ourSlot:
            return

        # Lock/Unlock slot
        match.toggleSlotLocked(multiplayer_match["match_id"], packetData["slotID"])
