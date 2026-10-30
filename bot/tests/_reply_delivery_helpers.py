"""Transport boundary for reply unit cases; inventory has PostgreSQL coverage."""


async def tracked_reply_send(bot, *, kind, appeal_id, max_user_ids, **kwargs):
    assert kind == "operator_part"
    assert appeal_id > 0
    assert max_user_ids == [kwargs["user_id"]]
    return await bot.send_message(**kwargs)
