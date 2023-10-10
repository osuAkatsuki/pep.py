from __future__ import annotations

import asyncio
import logging

from objects import osuToken
from objects.redisLock import redisLock

# TODO: this should be used in other places in the code
# and potentially abstracted into a more appropriate place
CHAT_SPAM_SAMPLE_INTERVAL = 10  # seconds


async def reset_all_users_spam_rate() -> None:
    """bancho-service silences users by tracking how"""
    logging.info("Starting spam protection loop")
    while True:
        for token_id in await osuToken.get_token_ids():
            async with redisLock(
                f"{osuToken.make_key(token_id)}:processing_lock",
            ):
                await osuToken.update_token(token_id, spam_rate=0)

        await asyncio.sleep(CHAT_SPAM_SAMPLE_INTERVAL)
