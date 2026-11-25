import datetime
import logging

import pytz
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def _delete_message_cb(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        await context.bot.delete_message(job.chat_id, job.data)
    except Exception:
        # 消息可能已经被用户删了，或超过 48 小时无法删除，属正常
        pass


async def delete_message_later(
    delay: float, chat_id, msg_id: int, context: ContextTypes.DEFAULT_TYPE
) -> str:
    name = f"deljob_{chat_id}_{msg_id}"
    context.job_queue.run_once(
        _delete_message_cb, delay, chat_id=chat_id, name=name, data=msg_id
    )
    return name


async def _ban_user_cb(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id_str, minutes_str = job.data.split("-")
    ban_minutes = int(minutes_str)
    ban_until = datetime.datetime.now(pytz.utc) + datetime.timedelta(minutes=ban_minutes)
    try:
        await context.bot.ban_chat_member(job.chat_id, int(user_id_str), ban_until)
    except Exception as e:
        logger.warning(f"ban user failed: {e}")


async def ban_user_later(
    delay: float, chat_id, user_id: int, minutes: int, context: ContextTypes.DEFAULT_TYPE
) -> str:
    name = f"banjob_{chat_id}_{user_id}"
    context.job_queue.run_once(
        _ban_user_cb, delay, chat_id=chat_id, name=name, data=f"{user_id}-{minutes}"
    )
    return name


def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """移除同名任务，返回是否真的移除了。"""
    jobs = context.job_queue.get_jobs_by_name(name)
    if not jobs:
        return False
    for job in jobs:
        job.schedule_removal()
    return True
