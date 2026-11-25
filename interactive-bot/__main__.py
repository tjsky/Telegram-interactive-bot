import asyncio
import io
import os
import re
import time
import types
from contextlib import contextmanager
from datetime import datetime, time as dtime

import pytz
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    EndPointNotFound,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import mention_html

from db.database import SessionMaker, engine, ensure_indexes, ensure_schema
from db.model import Base, FormnStatus, MediaGroupMesssage, MessageMap, User, UserPref

from . import (
    ASSETS_DIR,
    BASE_DIR,
    admin_group_id,
    admin_user_ids,
    app_name,
    bot_token,
    disable_captcha,
    is_delete_topic_as_ban_forever,
    is_delete_user_messages,
    logger,
    message_interval,
    welcome_message,
)
from .captcha_engine import (
    CaptchaConfig,
    CaptchaEngine,
    ChallengeStore,
    QuotaGuard,
    Verdict,
)
from .spam_filter import (
    MsgInfo,
    SpamFilter,
    SpamStore,
    build_daily_report,
    preview_line,
    strip_links,
)
from .utils import delete_message_later

# 建表：create_all 只补缺失的表，加列由 ensure_schema() 负责
Base.metadata.create_all(bind=engine)
ensure_schema()
ensure_indexes()

TZ = pytz.timezone(os.getenv("BOT_TIMEZONE", "Asia/Shanghai"))

# 管理员判定统一走这里：先 int 化，避免字符串 ID 静默判不中
_ADMIN_IDS = frozenset(int(x) for x in admin_user_ids)


def _is_admin(uid) -> bool:
    try:
        return int(uid) in _ADMIN_IDS
    except (TypeError, ValueError):
        return False


# 字体跟代码走，不放 assets/（部署时 assets 常被挂成持久卷）
_FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "fonts")

# 内置字体（SIL OFL 1.1），四种字形风格刻意拉开
_BUNDLED_FONTS = (
    "Inter-Bold.otf",
    "JetBrainsMono-Bold.ttf",
    "ComicNeue-Bold.ttf",
    "ZillaSlab-Bold.ttf",
)

# 系统兜底只认自由字体，微软字体有版权问题
_SYSTEM_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts",
)
_SYSTEM_FONT_NAMES = (
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
)


def _collect_fonts() -> list[str]:
    """字体池：项目自带的优先，系统自由字体兜底。"""
    bundled = [
        os.path.join(_FONT_DIR, fn)
        for fn in _BUNDLED_FONTS
        if os.path.exists(os.path.join(_FONT_DIR, fn))
    ]
    if bundled:
        return bundled

    # 字体目录被清空时别直接崩
    fallback: list[str] = []
    for d in _SYSTEM_FONT_DIRS:
        for fn in _SYSTEM_FONT_NAMES:
            path = os.path.join(d, fn)
            if os.path.exists(path):
                fallback.append(path)
        if len(fallback) >= 2:
            break
    return fallback


_captcha_fonts = _collect_fonts()
if _captcha_fonts:
    logger.info(
        "验证码字体 %d 个：%s",
        len(_captcha_fonts),
        "、".join(os.path.basename(f) for f in _captcha_fonts),
    )
else:
    # 字体缺失只告警不退出
    logger.error("fonts/ 目录下没有可用字体，验证码将退化为默认字体，请检查部署包")

captcha_engine = CaptchaEngine(
    CaptchaConfig(
        font_paths=_captcha_fonts,
        charset="23456789ACDEFHJKMNPQRUVWXY",
        length=6,
        cooldown_ladder=(0.0, 30.0, 120.0, 600.0, 3600.0, 86400.0),
        max_fails=6,
    ),
    ChallengeStore(os.path.join(ASSETS_DIR, "captcha.db")),
)

spam_store = SpamStore(os.path.join(ASSETS_DIR, "spam.db"))
spam_filter = SpamFilter(store=spam_store)
# 配额：10 分钟 30 条；新号首小时 10 条
quota = QuotaGuard(window=600, max_messages=30, new_user_window=3600, new_user_max=10)

REVIEW_TOPIC_NAME = "⚠️ 待审核"


# 每个操作一个 Session：共用会在 await 点串数据，一次异常后还会整体卡死
@contextmanager
def db_session():
    s = SessionMaker()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# 同一用户串行，不同用户并行
_user_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def user_lock(uid: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _user_locks.get(uid)
        if lock is None:
            lock = _user_locks[uid] = asyncio.Lock()
        return lock


def safe_topic_name(tg_user) -> str:
    """话题标题只能用「清洗过的昵称 + id」。"""
    name = strip_links(tg_user.full_name or "")
    name = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name or any(k in name.lower() for k in spam_filter.cfg.strong_keywords):
        # 昵称本身就是广告的，直接不给展示位
        name = "用户"
    return f"{name[:16]} | {tg_user.id}"


def user_exists(uid: int) -> bool:
    with db_session() as s:
        return s.query(User).filter(User.user_id == uid).first() is not None


# 网络抖动重试：只重试可恢复的错误

_RETRYABLE = (TimedOut, NetworkError)
# BadRequest 是 NetworkError 的子类，不能按类型一概重试
_PERMANENT = (
    BadRequest,
    Forbidden,
    Conflict,
    ChatMigrated,
    InvalidToken,
    EndPointNotFound,
)


def _is_retryable(err: BaseException) -> bool:
    if isinstance(err, _PERMANENT):
        return False
    if isinstance(err, _RETRYABLE):
        return True
    return _looks_like_network(err)


async def _retry(factory, attempts: int = 3, base_delay: float = 0.8):
    """执行 factory()，遇到可恢复的网络错误退避重试。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await factory()
        except RetryAfter as e:
            last = e
            delay = min(e.retry_after + 0.5, 30.0)
            logger.info("被 Telegram 限速，等待 %.1fs 后重试", delay)
        except Exception as e:
            if not _is_retryable(e):
                raise
            last = e
            delay = base_delay * (2**i)
            logger.info(
                "网络错误 %s，%.1fs 后重试（%d/%d）",
                type(e).__name__,
                delay,
                i + 1,
                attempts,
            )
        if i + 1 < attempts:
            await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


def _looks_like_network(err: BaseException) -> bool:
    """没被 PTB 包住的底层网络异常（httpx.ReadError 这类）。"""
    mod = type(err).__module__ or ""
    return mod.startswith("httpx") or mod.startswith("httpcore")


def _is_thread_gone(err: BaseException) -> bool:
    """话题被删掉之后 Telegram 的报错。"""
    if not isinstance(err, BadRequest):
        return False
    text = str(err).lower()
    return (
        "thread not found" in text
        or "topic_id_invalid" in text
        or "topic deleted" in text
    )


async def _copy_into_topic(
    context: ContextTypes.DEFAULT_TYPE,
    uid,
    tg_user,
    thread_id,
    from_chat_id,
    message_id,
    recreate: bool = True,
    **params,
):
    """把消息复制进后台话题，话题没了就自动重建。"""

    async def call(tid):
        return await context.bot.copy_message(
            chat_id=admin_group_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            message_thread_id=tid,
            **params,
        )

    try:
        return await _retry(lambda: call(thread_id)), thread_id
    except BadRequest as e:
        if not _is_thread_gone(e) or not recreate:
            raise
        logger.info("话题已不存在，自动重建 uid=%s old_thread=%s", uid, thread_id)

    new_thread = await _reopen_thread(context, uid, thread_id, tg_user)
    return await _retry(lambda: call(new_thread)), new_thread


async def safe_reply(update: Update, text: str, **kwargs):
    """回消息失败不能连带把 handler 打挂。"""
    try:
        await _retry(lambda: update.effective_chat.send_message(text, **kwargs))
    except Exception as e:
        logger.warning(f"safe_reply failed: {e}")


# 验证前先扣住消息，通过后补发；只放内存
_PENDING_TTL = 15 * 60          # 超过这个时间的旧消息不再补发
_PENDING_MAX = 5                # 每个用户最多扣几条
_PENDING_USERS = 2000           # 全局最多跟踪多少个未验证用户
_pending: dict[int, list[tuple[float, object]]] = {}

GATE_PASS = "pass"              # 已通过验证，本条正常转发
GATE_STOP = "stop"              # 本条已被消费（发图 / 提示 / 冷却）
GATE_VERIFIED = "verified"      # 刚通过验证，本条是答案，缓存的消息交给调用方补发


def _buffer_message(uid: int, msg) -> None:
    now = time.time()
    items = [it for it in _pending.get(uid, []) if now - it[0] < _PENDING_TTL]
    items.append((now, msg))
    _pending[uid] = items[-_PENDING_MAX:]

    if len(_pending) > _PENDING_USERS:
        # 未验证用户可能很多，超量就按最后活跃时间淘汰最旧的一批
        stale = sorted(_pending, key=lambda k: _pending[k][-1][0])
        for k in stale[: len(_pending) - _PENDING_USERS]:
            _pending.pop(k, None)


def _take_buffered(uid: int) -> list:
    now = time.time()
    return [m for ts, m in _pending.pop(uid, []) if now - ts < _PENDING_TTL]


def _looks_like_answer(text: str) -> bool:
    """这句话像不像验证码答案。"""
    raw = "".join((text or "").split())
    if len(raw) != captcha_engine.cfg.length:
        return False
    return all(c in captcha_engine.cfg.charset for c in raw.upper())


async def _nag(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, gap: float = 10.0
) -> None:
    """提醒类回复做节流。一个相册能连来 5 条消息，逐条回就成了刷屏。"""
    now = time.time()
    if now - context.user_data.get("_nag_at", 0) < gap:
        return
    context.user_data["_nag_at"] = now
    await safe_reply(update, text)


class _Replay:
    """把一条缓存消息包成 Update 的样子，好复用完整的转发流程。"""

    __slots__ = (
        "message",
        "effective_user",
        "effective_chat",
        "edited_message",
        "callback_query",
    )

    def __init__(self, message, user):
        self.message = message
        self.effective_user = user
        self.effective_chat = message.chat
        self.edited_message = None
        self.callback_query = None


async def send_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ch = captcha_engine.issue(user.id)

    # 每次重试都要新建 InputFile：buffer 只能读一次
    async def _send():
        return await context.bot.send_photo(
            update.effective_chat.id,
            photo=InputFile(io.BytesIO(ch.image), filename="captcha.png"),
            caption=(
                f"{mention_html(user.id, user.first_name)}\n\n"
                f"请直接发送图片里的 {len(ch.answer)} 个字符。\n"
                "你之前发的消息，验证通过后会自动转发，不用重发。"
            ),
            parse_mode="HTML",
        )

    sent = await _retry(_send)
    await delete_message_later(captcha_engine.cfg.ttl, sent.chat.id, sent.message_id, context)


async def check_human(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """验证门禁，返回 GATE_* 三种结果之一。"""
    if disable_captcha:
        return GATE_PASS

    user = update.effective_user
    uid = user.id

    if captcha_engine.is_human(uid):
        return GATE_PASS

    msg = update.message

    # 冷却 / 拉黑检查必须在生成图片之前，否则高频消息会不停刷图
    left = captcha_engine.cooldown_left(uid)
    if left < 0:
        await safe_reply(update, "账号已被限制，如有疑问请通过其他渠道联系。")
        return GATE_STOP
    if left > 0:
        mins, secs = int(left // 60), int(left % 60)
        wait = f"{mins} 分 {secs} 秒" if mins else f"{secs} 秒"
        await _nag(update, context, f"请等待 {wait} 后再试。")
        return GATE_STOP

    ch = captcha_engine.store.peek_challenge(uid)
    if ch is None or time.time() - ch.issued_at > captcha_engine.cfg.ttl:
        if msg is not None:
            _buffer_message(uid, msg)
        await send_challenge(update, context)
        return GATE_STOP

    text = msg.text if msg else ""
    if not _looks_like_answer(text):
        # 不是答案就是随口另一句，扣住别算答错
        if msg is not None:
            _buffer_message(uid, msg)
        await _nag(update, context, "请先完成验证：直接发送图片里的字符。")
        return GATE_STOP

    result = captcha_engine.verify_text(uid, text)

    if result.verdict is Verdict.OK:
        # 这条是答案，消费掉；清令牌桶，否则紧接着的第一句会被压
        _buckets.pop(uid, None)
        return GATE_VERIFIED

    if result.verdict is Verdict.HONEYPOT:
        # 同一答案发给多个号 = 批量破解，影子封禁
        context.user_data["shadow_banned"] = True
        _buckets.pop(uid, None)
        return GATE_VERIFIED

    if result.verdict is Verdict.BANNED:
        await safe_reply(update, "验证失败次数过多，账号已被限制。")
        return GATE_STOP

    if result.verdict is Verdict.EXPIRED:
        if msg is not None:
            _buffer_message(uid, msg)
        await send_challenge(update, context)
        return GATE_STOP

    # WRONG：挑战已消费，下一条消息会拿到新图
    await safe_reply(update, f"答案不正确，请等待 {int(result.cooldown_left)} 秒后重试。")
    return GATE_STOP


# 相册是 N 条共用 media_group_id 的更新，攒齐一次发出，静默窗口 3 秒；
# 服务端原子创建，同组 message_id 必连续，断号说明还没到齐。
_MEDIA_GROUP_DELAY = 3.0
_MEDIA_GROUP_GAP_RETRY = 2.0
_MEDIA_GROUP_MAX_WAIT = 15.0

# 刚发过的组记 120 秒，迟到的尾巴不算新相册
_sent_groups: dict[str, float] = {}
_LATE_ALBUM_GRACE = 120.0

# 只在一条都没发出去时重试整组
_MEDIA_GROUP_RETRY_DELAY = 30.0
_MEDIA_GROUP_MAX_RETRY = 2

# sendMediaGroup 只支持这四种，其余整组退回逐条复制
_ALBUM_KINDS = ("photo", "video", "document", "audio")


def _group_key(chat_id, media_group_id) -> str:
    return f"{chat_id}:{media_group_id}"


def _album_item(msg) -> tuple[str, str]:
    """取出这一条相册元素的 (类型, file_id)。"""
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.document:
        return "document", msg.document.file_id
    if msg.audio:
        return "audio", msg.audio.file_id
    return "", ""


def _album_media(items):
    kinds = {
        "photo": InputMediaPhoto,
        "video": InputMediaVideo,
        "document": InputMediaDocument,
        "audio": InputMediaAudio,
    }
    media = []
    for it in items:
        kwargs = {"media": it["file_id"]}
        if it["caption"]:
            kwargs["caption"] = it["caption"]
            # parse_mode 要写在每个 InputMedia 上，组级参数管不到元素 caption
            kwargs["parse_mode"] = "HTML"
        media.append(kinds[it["media_type"]](**kwargs))
    return media


async def _send_media_group_later(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data if isinstance(job.data, dict) else {}
    media_group_id = data.get("media_group_id")
    from_chat_id = data.get("from_chat_id")
    target_id = data.get("target_id")
    direction = data.get("direction")
    if media_group_id is None or from_chat_id is None or target_id is None:
        return

    with db_session() as s:
        rows = (
            s.query(MediaGroupMesssage)
            .filter(
                MediaGroupMesssage.media_group_id == media_group_id,
                MediaGroupMesssage.chat_id == from_chat_id,
            )
            .order_by(MediaGroupMesssage.message_id)
            .all()
        )
        items = [
            {
                "message_id": m.message_id,
                "file_id": m.file_id or "",
                "media_type": m.media_type or "",
                "caption": m.caption_html or "",
            }
            for m in rows
        ]
    if not items:
        return

    # 断号 = 中间那张还在路上，稍等；有总时长上限，防更新真丢
    ids = [i["message_id"] for i in items]
    holed = any(b - a > 1 for a, b in zip(ids, ids[1:]))
    if holed:
        now = time.time()
        first_seen = float(data.get("first_seen") or now)
        if now - first_seen < _MEDIA_GROUP_MAX_WAIT:
            logger.info(
                "相册缺号，暂缓发送 group=%s ids=%s 已等 %.0fs",
                media_group_id,
                ids,
                now - first_seen,
            )
            context.job_queue.run_once(
                _send_media_group_later,
                _MEDIA_GROUP_GAP_RETRY,
                name=job.name,
                data={**data, "first_seen": first_seen},
            )
            return
        logger.warning(
            "相册缺号等满 %.0fs 仍未补齐，按现有 %d 条发送 group=%s ids=%s",
            _MEDIA_GROUP_MAX_WAIT,
            len(ids),
            media_group_id,
            ids,
        )

    thread_id = None
    if direction == "u2a":
        with db_session() as s:
            u = s.query(User).filter(User.user_id == from_chat_id).first()
            thread_id = u.message_thread_id if u else None

    async def deliver(tid):
        """整组发一次，失败就退回逐条复制。返回 (结果列表, 最后一个异常)。"""
        err = None
        # 用 sendMediaGroup 重建相册；copyMessages 会散成 N 条
        if len(items) >= 2 and all(
            i["media_type"] in _ALBUM_KINDS and i["file_id"] for i in items
        ):
            try:
                out = await _retry(
                    lambda: context.bot.send_media_group(
                        chat_id=target_id,
                        media=_album_media(items),
                        message_thread_id=tid,
                    ),
                    attempts=3,
                )
                return list(out), None
            except Exception as e:
                err = e
                logger.warning(
                    "相册整组发送失败，退回逐条复制 group=%s dir=%s: %s",
                    media_group_id,
                    direction,
                    e,
                )

        # 兜底逐条复制，宁散不丢
        out = []
        for it in items:
            try:
                out.append(
                    await _retry(
                        lambda mid=it["message_id"]: context.bot.copy_message(
                            chat_id=target_id,
                            from_chat_id=from_chat_id,
                            message_id=mid,
                            message_thread_id=tid,
                        )
                    )
                )
            except Exception as e:
                logger.warning(
                    "相册逐条补发失败 group=%s msg=%s: %s",
                    media_group_id,
                    it["message_id"],
                    e,
                )
                out.append(None)
                err = err or e
        return out, err

    sent, err = await deliver(thread_id)

    # 定时任务不经过 _copy_into_topic，话题没了要自己重建
    if (
        direction == "u2a"
        and not is_delete_topic_as_ban_forever
        and err is not None
        and _is_thread_gone(err)
        and not any(x is not None for x in sent)
    ):
        logger.info(
            "话题已不存在，重建后重发相册 uid=%s old_thread=%s", from_chat_id, thread_id
        )
        tg_user = _user_stub(from_chat_id, _load_user(from_chat_id))
        thread_id = await _reopen_thread(context, from_chat_id, thread_id or 0, tg_user)
        sent, err = await deliver(thread_id)

    owner = from_chat_id if direction == "u2a" else target_id
    pairs = []
    for it, out in zip(items, sent):
        if out is None:
            continue
        if direction == "u2a":
            pairs.append((it["message_id"], out.message_id))
        else:
            pairs.append((out.message_id, it["message_id"]))

    if not pairs:
        attempt = int(data.get("attempt", 0))
        if attempt < _MEDIA_GROUP_MAX_RETRY:
            logger.warning(
                "相册整组未发出，%d 秒后重试（%d/%d）group=%s",
                int(_MEDIA_GROUP_RETRY_DELAY),
                attempt + 1,
                _MEDIA_GROUP_MAX_RETRY,
                media_group_id,
            )
            context.job_queue.run_once(
                _send_media_group_later,
                _MEDIA_GROUP_RETRY_DELAY,
                name=f"{job.name}_r{attempt + 1}",
                data={**data, "attempt": attempt + 1},
            )
            return
        logger.error(
            "相册重试 %d 次仍失败，放弃 group=%s dir=%s",
            _MEDIA_GROUP_MAX_RETRY,
            media_group_id,
            direction,
        )

    with db_session() as s:
        for user_msg, group_msg in pairs:
            s.add(
                MessageMap(
                    user_chat_message_id=user_msg,
                    group_chat_message_id=group_msg,
                    user_id=owner,
                )
            )
        # 发完即删，避免重复转发和无限增长
        s.query(MediaGroupMesssage).filter(
            MediaGroupMesssage.media_group_id == media_group_id,
            MediaGroupMesssage.chat_id == from_chat_id,
        ).delete()

    # 记下刚发过的组，供迟到元素识别
    if pairs:
        now = time.time()
        _sent_groups[_group_key(from_chat_id, media_group_id)] = now
        if len(_sent_groups) > 500:
            for k in [
                k for k, ts in _sent_groups.items() if now - ts > _LATE_ALBUM_GRACE
            ]:
                _sent_groups.pop(k, None)


async def send_media_group_later(
    delay: float, chat_id, target_id, media_group_id, direction, context
):
    # 每来一张就重排，发送时刻 = 最后一张 + delay
    name = f"mg_{chat_id}_{target_id}_{direction}_{media_group_id}"
    # first_seen 要跨重排传递，否则缺号能无限等
    first_seen = 0.0
    for old in context.job_queue.get_jobs_by_name(name):
        if isinstance(old.data, dict):
            first_seen = first_seen or float(old.data.get("first_seen") or 0.0)
        old.schedule_removal()
    context.job_queue.run_once(
        _send_media_group_later,
        delay,
        name=name,
        data={
            "media_group_id": media_group_id,
            "from_chat_id": chat_id,
            "target_id": target_id,
            "direction": direction,
            "first_seen": first_seen or time.time(),
        },
    )
    return name


async def send_contact_card(chat_id, message_thread_id, tg_user, context: ContextTypes):
    buttons = []
    if tg_user.username:
        buttons.append(
            [InlineKeyboardButton("👤 直接联络", url=f"https://t.me/{tg_user.username}")]
        )
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    caption = (
        f"👤 {mention_html(tg_user.id, tg_user.full_name)}\n\n"
        f"📱 <code>{tg_user.id}</code>\n\n"
        f"🔗 @{tg_user.username if tg_user.username else '无'}"
    )

    try:
        photos = await context.bot.get_user_profile_photos(tg_user.id)
        if photos.total_count:
            await context.bot.send_photo(
                chat_id,
                photo=photos.photos[0][-1].file_id,
                caption=caption,
                message_thread_id=message_thread_id,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
    except Exception as e:
        # 没头像、被拉黑都会走这里，不能让它把 handler 带崩
        logger.warning(f"profile photo failed for {tg_user.id}: {e}")

    try:
        await context.bot.send_message(
            chat_id,
            caption,
            message_thread_id=message_thread_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"contact card failed for {tg_user.id}: {e}")


async def ensure_thread(context: ContextTypes.DEFAULT_TYPE, tg_user) -> int:
    """拿到用户的话题 id，没有就建一个。调用方必须已持有该用户的锁。"""
    uid = tg_user.id
    with db_session() as s:
        u = s.query(User).filter(User.user_id == uid).first()
        if u and u.message_thread_id:
            return u.message_thread_id

    formn = await _retry(
        lambda: context.bot.create_forum_topic(admin_group_id, name=safe_topic_name(tg_user)),
        attempts=2,
    )
    thread_id = formn.message_thread_id

    with db_session() as s:
        u = s.query(User).filter(User.user_id == uid).first()
        if u is None:
            u = User(user_id=uid, message_thread_id=thread_id)
            s.add(u)
        else:
            u.message_thread_id = thread_id
        u.first_name = tg_user.first_name
        u.last_name = tg_user.last_name
        u.username = tg_user.username

    # 话题已建好，提示发不出去不能反过来算失败
    try:
        await _retry(
            lambda: context.bot.send_message(
                admin_group_id,
                f"新的会话 {mention_html(uid, tg_user.full_name)}",
                message_thread_id=thread_id,
                parse_mode="HTML",
            ),
            attempts=2,
        )
    except Exception as e:
        logger.warning(f"new conversation notice failed uid={uid}: {e}")
    await send_contact_card(admin_group_id, thread_id, tg_user, context)
    return thread_id


def _load_user(uid: int):
    """取一条 User 行。expire_on_commit=False，出块之后仍可读字段。"""
    with db_session() as s:
        return s.query(User).filter(User.user_id == uid).first()


def _user_stub(uid: int, row=None):
    """从库里的 User 行拼一个只带昵称字段的替身。"""
    first = (getattr(row, "first_name", "") or "") if row else ""
    last = (getattr(row, "last_name", "") or "") if row else ""
    return types.SimpleNamespace(
        id=uid,
        first_name=first,
        last_name=last,
        username=getattr(row, "username", None) if row else None,
        full_name=(f"{first} {last}").strip() or str(uid),
    )


async def _reopen_thread(context: ContextTypes.DEFAULT_TYPE, uid, old_thread, tg_user) -> int:
    """话题被后台删掉之后重建。"""
    with db_session() as s:
        u = s.query(User).filter(User.user_id == uid).first()
        if u:
            u.message_thread_id = 0
        s.query(FormnStatus).filter(FormnStatus.message_thread_id == old_thread).delete()

    return await ensure_thread(context, tg_user)


def _is_stopped(uid: int) -> bool:
    with db_session() as s:
        p = s.query(UserPref).filter(UserPref.user_id == uid).first()
        return bool(p and p.stopped)


def _set_stopped(uid: int, value: bool) -> bool:
    """写入停止状态，返回是否真的发生了变化。"""
    with db_session() as s:
        p = s.query(UserPref).filter(UserPref.user_id == uid).first()
        if p is None:
            if not value:
                return False
            s.add(UserPref(user_id=uid, stopped=True))
            return True
        if bool(p.stopped) == value:
            return False
        p.stopped = value
        return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # 从 /stop 恢复
    resumed = _set_stopped(user.id, False)

    if _is_admin(user.id):
        try:
            bg = await context.bot.get_chat(admin_group_id)
            if bg.type in ("supergroup", "group"):
                await update.message.reply_html(
                    f"你好管理员 {user.first_name}({user.id})\n\n"
                    f"欢迎使用 {app_name}。\n\n"
                    f"目前配置正确，可以在群组 <b>{bg.title}</b> 中使用。"
                )
                return
        except Exception as e:
            logger.error(f"admin group error {e}")
            await update.message.reply_html(
                f"⚠️ 后台管理群组设置错误，请检查配置。\n"
                f"需要确保已将 @{context.bot.username} 邀请入群并授予管理员权限。"
            )
            return
        await update.message.reply_html(f"你好管理员 {user.first_name}({user.id})。")
        return

    await update.message.reply_html(
        f"{'已恢复接收 ✅\n\n' if resumed else ''}"
        f"{mention_html(user.id, user.full_name)} 你好：\n\n{welcome_message}"
    )


# 文案分两份：管理命令只出现在 _HELP_ADMIN

_HELP_USER = """<b>怎么用</b>

{captcha}之后直接发消息就行 —— 文字、图片、文件、语音、相册都会被原样转发，
对方的回复会由 bot 发给你，正常聊天即可。

PS：Bot 有反刷屏和反 SPAM 机制，正常聊天应该不会触发，如果有误触请及时反馈。

<b>命令</b>
/help — 帮助文档
/privacy — 隐私协议
/stop — 停止对话（重新 /start 前都无法对话）
/start — 开始对话"""

# 验证码关掉的时候（DISABLE_CAPTCHA=TRUE）这句不能出现，否则是说谎
_CAPTCHA_LINE = "第一次联系的时候，需要先通过人机验证：正确回复图片里的验证码字符。\n\n"

_HELP_ADMIN = """<b>管理员命令</b>

/help — 帮助文档
/clear — 在对应的话题里发送：关闭该会话并删除话题
/broadcast — 回复一条消息再发送：把它广播给全部用户（除非你有广播需求，否则慎用）
/release_user 用户ID — 解封用户（用于解封被误判 SPAM 和验证码错误次数太多的）

<b>日常操作</b>

• 在话题里回复某条消息 = 回复对应用户
• 直接删话题 = 关闭与对应用户的会话。若开启 <code>DELETE_TOPIC_AS_FOREVER_BAN=TRUE</code>
  则会永久封禁对应用户，禁止他经 bot 和你聊天
• 每天 09:00 会发一条前一天的拦截情况汇总；每天 04:00 清理过期数据"""

_PRIVACY = """<b>我们目前收集并处理以下信息：</b>

• <b>您的公开 Telegram 信息</b> — Telegram 用户 ID、名字、姓氏、用户名。
  （注意：这是您在 Telegram 自行设置并公开的信息，我们不知道您的“真实”信息。）
• <b>您消息的指纹</b> — 用来识别和标记 SPAM 广告消息。正常消息只存指纹、不存消息，
  30 天后删除；被判为广告的保留 180 天。
• <b>被判定为广告的消息原文</b> — 正常消息不存原文，只存被判定为疑似广告的。
  保留 180 天，用于复核和误封申诉。
• <b>人机验证状态</b> — 这个不存就每次都需要做人机验证了。
• <b>图片、视频、文件的 file_id</b> — 你发的图片、视频、文件不会下载到 Bot 的服务器上，
  文件本体都在 Telegram 服务器上，Bot 只做转发。
• 注意：Bot 不存你的消息，但你发给对方的消息，只要对方不删就永远都在。
  Bot 本身没有撤回消息的机制。

<b>停止对话与删除信息</b>

• /stop 之后 Bot 不再转发你的消息，也不会主动给你发消息（重新发 /start 前都无法对话）。
• 想删掉已保存的信息（对照表、指纹、拦截记录），直接对我说一声，管理员会清理。"""


def _help_user_text() -> str:
    captcha = "" if disable_captcha else _CAPTCHA_LINE
    return _HELP_USER.replace("{captcha}", captcha)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """同一个 /help，按身份给两份不同的文案。"""
    user = update.effective_user
    admin = _is_admin(user.id)
    # 打一行日志，方便排查身份判定
    logger.info(f"help uid={user.id} admin={admin}")
    text = _HELP_ADMIN if admin else _help_user_text()
    kwargs = {"parse_mode": "HTML"}
    # 在话题里问的就在话题里答，别飘到主群
    thread_id = getattr(update.message, "message_thread_id", None)
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    await safe_reply(update, text, **kwargs)


async def privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, _PRIVACY, parse_mode="HTML")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    _take_buffered(uid)
    _set_stopped(uid, True)
    logger.info(f"user stopped uid={uid}")
    await safe_reply(
        update,
        "已停止 ✅\n\n"
        "之后你发的消息我不会再转发，也不会主动给你发消息。\n"
        "想恢复随时发 /start。",
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        return
    thread_id = update.message.message_thread_id
    if not thread_id:
        await safe_reply(update, "请在具体的话题里使用 /clear。")
        return

    target_user_id = None
    with db_session() as s:
        if u := s.query(User).filter(User.message_thread_id == thread_id).first():
            target_user_id = u.user_id
            u.message_thread_id = 0
        s.query(FormnStatus).filter(FormnStatus.message_thread_id == thread_id).delete()

    try:
        await context.bot.delete_forum_topic(update.effective_chat.id, thread_id)
    except Exception as e:
        logger.warning(f"delete topic failed: {e}")

    if not is_delete_user_messages or not target_user_id:
        return

    with db_session() as s:
        msg_ids = [
            m.user_chat_message_id
            for m in s.query(MessageMap).filter(MessageMap.user_id == target_user_id).all()
        ]
        # 映射表一起删，否则只增不减
        s.query(MessageMap).filter(MessageMap.user_id == target_user_id).delete()

    for i in range(0, len(msg_ids), 100):
        try:
            await context.bot.delete_messages(target_user_id, msg_ids[i : i + 100])
        except Exception as e:
            logger.warning(f"delete user messages failed: {e}")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        return
    if not update.message.reply_to_message:
        await safe_reply(update, "这条指令需要回复一条消息，被回复的消息将被广播。")
        return
    context.job_queue.run_once(
        _broadcast,
        0,
        data=f"{update.message.reply_to_message.id}_{update.effective_chat.id}",
    )


async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    msg_id, chat_id = context.job.data.split("_")
    with db_session() as s:
        users = [u.user_id for u in s.query(User).all()]

    done = failed = 0
    for uid in users:
        if _is_admin(uid):
            continue
        for _ in range(3):
            try:
                await context.bot.copy_message(uid, chat_id, int(msg_id))
                done += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                failed += 1
                break
            except Exception as e:
                logger.debug(f"broadcast to {uid} failed: {e}")
                failed += 1
                break
        # 主动节流，约 20 条/秒
        await asyncio.sleep(0.05)

    logger.info(f"broadcast done: {done} ok, {failed} failed")


async def release_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        return
    if not context.args:
        await safe_reply(update, "用法：/release_user <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await safe_reply(update, "user_id 必须是数字")
        return

    spam_store.release_user(uid)
    captcha_engine.store.reset(uid)
    with db_session() as s:
        if u := s.query(User).filter(User.user_id == uid).first():
            u.message_thread_id = 0
    await safe_reply(update, f"已放行 {uid}，验证状态与冷却已清除。")


async def daily_spam_report(context: ContextTypes.DEFAULT_TYPE):
    text = build_daily_report(spam_store)
    for chunk in _split_message(text, 3800):
        try:
            await context.bot.send_message(admin_group_id, chunk)
        except Exception as e:
            logger.warning(f"daily report failed: {e}")


async def daily_prune(context: ContextTypes.DEFAULT_TYPE):
    n = spam_store.prune()
    captcha_engine.store.prune()
    # 令牌桶按 uid 存，不清理会跟着用户数一直涨
    cutoff = time.time() - 86400
    for k in [k for k, (_, ts) in _buckets.items() if ts < cutoff]:
        _buckets.pop(k, None)
    logger.info(f"pruned {n} spam fingerprints")


def _split_message(text: str, limit: int) -> list[str]:
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


async def _ensure_review_topic(context: ContextTypes.DEFAULT_TYPE):
    cached = context.bot_data.get("review_thread_id")
    if cached:
        return cached
    try:
        topic = await context.bot.create_forum_topic(admin_group_id, REVIEW_TOPIC_NAME)
        context.bot_data["review_thread_id"] = topic.message_thread_id
        return topic.message_thread_id
    except Exception as e:
        logger.warning(f"create review topic failed: {e}")
        return None


async def _maybe_escalate(uid: int, tg_user, context: ContextTypes.DEFAULT_TYPE):
    """同一用户当天被静默拦满 N 条，推一次到待审核话题。"""
    day_start = datetime.now(TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    if spam_store.count_today_blocks(uid, day_start) != spam_filter.cfg.review_after_blocks:
        return

    rows = [r for r in spam_store.blocks_since(day_start) if r["user_id"] == uid]
    if not rows:
        return

    thread_id = await _ensure_review_topic(context)
    lines = [
        "⚠️ 疑似误封，请确认",
        f"{mention_html(uid, tg_user.full_name)}  ({uid})",
        f"今日已被静默拦截 {len(rows)} 条，最近几条：",
    ]
    for r in rows[-3:]:
        lines.append(f"「{preview_line(r['text'])}」")
    lines.append("")
    lines.append(f"放行：/release_user {uid}")
    try:
        await context.bot.send_message(
            admin_group_id,
            "\n".join(lines),
            message_thread_id=thread_id,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"escalate failed: {e}")


# 同时处理的更新上限，超出说明是脚本在灌
_BURST_MAX = 30
_inflight: dict[int, int] = {}


# 令牌桶：桶里 8 个，前 8 条直接放行，之后每 MESSAGE_INTERVAL 秒补 1 个；
# 固定间隔会把「连发 5 张图」拖成 20 秒。
_BUCKET_SIZE = 8
_BUCKET_USERS = 5000
_buckets: dict[int, tuple[float, float]] = {}  # uid -> (剩余令牌, 上次更新时间)


def _prune_buckets(now: float) -> None:
    if len(_buckets) <= _BUCKET_USERS:
        return
    cutoff = now - 3600
    for k in [k for k, (_, ts) in _buckets.items() if ts < cutoff]:
        _buckets.pop(k, None)


async def _rate_limit(uid: int) -> None:
    if not message_interval:
        return
    now = time.time()
    _prune_buckets(now)

    tokens, ts = _buckets.get(uid, (float(_BUCKET_SIZE), now))
    tokens = min(float(_BUCKET_SIZE), tokens + (now - ts) / message_interval)
    if tokens >= 1:
        _buckets[uid] = (tokens - 1, now)
        return

    wait = (1 - tokens) * message_interval
    _buckets[uid] = (0.0, now + wait)
    await asyncio.sleep(wait)


# 硬拒绝，但提示 5 分钟一次就够
_QUOTA_NOTICE_GAP = 300.0
_quota_notice: dict[int, float] = {}


async def _quota_reply(update: Update, uid: int, reason: str) -> None:
    now = time.time()
    last = _quota_notice.get(uid, 0.0)
    if now - last < _QUOTA_NOTICE_GAP:
        logger.info(f"quota-drop uid={uid}（已在 {int(now - last)} 秒前提示过）")
        return
    _quota_notice[uid] = now
    if len(_quota_notice) > 5000:
        cutoff = now - _QUOTA_NOTICE_GAP
        for k in [k for k, ts in _quota_notice.items() if ts < cutoff]:
            _quota_notice.pop(k, None)
    await safe_reply(update, reason)


def _album_started(msg) -> bool:
    """这一条是不是某个相册的后续元素（同组已经有落库记录）。"""
    if not msg.media_group_id:
        return False

    # 这一组刚发过 → 迟到的尾巴，不再算新相册
    key = _group_key(msg.chat.id, msg.media_group_id)
    sent_at = _sent_groups.get(key)
    if sent_at is not None:
        if time.time() - sent_at < _LATE_ALBUM_GRACE:
            logger.info(
                "相册迟到元素 group=%s msg=%s（该组已在 %.0f 秒前发出）",
                msg.media_group_id,
                msg.message_id,
                time.time() - sent_at,
            )
            return True
        _sent_groups.pop(key, None)

    with db_session() as s:
        return (
            s.query(MediaGroupMesssage.id)
            .filter(
                MediaGroupMesssage.media_group_id == msg.media_group_id,
                MediaGroupMesssage.chat_id == msg.chat.id,
            )
            .first()
            is not None
        )


async def forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id

    n = _inflight.get(uid, 0)
    if n >= _BURST_MAX:
        logger.info(f"burst-drop uid={uid} inflight={n}")
        return
    _inflight[uid] = n + 1
    try:
        lock = await user_lock(uid)
        async with lock:
            await _handle_u2a(update, context, user)
    finally:
        left = _inflight.get(uid, 1) - 1
        if left > 0:
            _inflight[uid] = left
        else:
            _inflight.pop(uid, None)


async def _handle_u2a(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user,
    replayed: bool = False,
):
    uid = user.id
    msg = update.message

    # 用户已 /stop，静默丢弃；命令本身由独立 handler 处理
    if _is_stopped(uid):
        logger.info(f"stopped uid={uid} dropped msg={msg.message_id}")
        return

    if context.user_data.get("shadow_banned"):
        logger.info(f"shadow-banned {uid} dropped")
        return

    # 只对已验证用户整形；相册整组跳过，靠定时任务合并
    if not replayed and not msg.media_group_id and captcha_engine.is_human(uid):
        await _rate_limit(uid)

    gate = await check_human(update, context)

    if gate == GATE_STOP:
        return

    if gate == GATE_VERIFIED:
        pending = _take_buffered(uid)
        if not pending:
            await safe_reply(update, "验证通过，请发送你的消息。")
            return
        await safe_reply(
            update,
            f"验证通过 ✅ 你刚才发的 {len(pending)} 条消息已经一并转发了，不用重发。",
        )
        for old in pending:
            try:
                await _handle_u2a(_Replay(old, user), context, user, replayed=True)
            except Exception as e:
                logger.exception(f"replay failed uid={uid}: {e}")
        return

    is_new = not user_exists(uid)
    text = msg.text or msg.caption or ""
    verdict = spam_filter.check(
        MsgInfo(
            user_id=uid,
            text=text,
            name=user.full_name,
            username=user.username or "",
            is_new_user=is_new,
            has_media=msg.effective_attachment is not None,
        )
    )

    if verdict.block:
        # 静默失败：不回复、不转发、不建话题
        logger.info(f"blocked uid={uid} score={verdict.score} {verdict.reasons}")
        await _maybe_escalate(uid, user, context)
        return

    # 补发的不计配额；相册整组算一条（同组已落库则直接放行）
    if not replayed and not _album_started(msg):
        ok, reason = quota.allow(uid, captcha_engine.store.get_state(uid)[3])
        if not ok:
            await _quota_reply(update, uid, reason)
            return

    thread_id = await ensure_thread(context, user)

    with db_session() as s:
        f = (
            s.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == thread_id)
            .first()
        )
        if f and f.status == "closed":
            await safe_reply(update, "对话已关闭，消息暂时无法送达。")
            return

    params = {}
    if msg.reply_to_message:
        with db_session() as s:
            m = (
                s.query(MessageMap)
                .filter(MessageMap.user_chat_message_id == msg.reply_to_message.message_id)
                .first()
            )
            if m:
                params["reply_to_message_id"] = m.group_chat_message_id
        # 被引用的消息可能已删，允许没有回复目标也照发
        params["allow_sending_without_reply"] = True

    if msg.media_group_id:
        media_type, file_id = _album_item(msg)
        with db_session() as s:
            s.add(
                MediaGroupMesssage(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    media_group_id=msg.media_group_id,
                    is_header=False,
                    caption_html=msg.caption_html,
                    media_type=media_type,
                    file_id=file_id,
                )
            )
        await send_media_group_later(
            _MEDIA_GROUP_DELAY, uid, admin_group_id, msg.media_group_id, "u2a", context
        )
        return

    try:
        sent, thread_id = await _copy_into_topic(
            context,
            uid,
            user,
            thread_id,
            update.effective_chat.id,
            msg.id,
            recreate=not is_delete_topic_as_ban_forever,
            **params,
        )
    except BadRequest as e:
        # 不是话题丢失（那种已重建），多半是类型不支持或权限不足
        logger.warning(f"u2a send failed uid={uid}: {e}")
        if _is_thread_gone(e):
            # 封禁模式，或重建话题本身也失败了
            if is_delete_topic_as_ban_forever:
                await safe_reply(update, "对话已被关闭。")
            else:
                await safe_reply(update, "会话重置失败，请稍后再发一次。")
        else:
            await safe_reply(update, "这条消息没能转过去，换个格式再发一次试试。")
        return
    except Forbidden:
        # bot 被踢出群 / 被降权
        logger.error("u2a forbidden: bot 在后台群没有发言权限 uid=%s", uid)
        await safe_reply(update, "消息暂时无法送达，请联系管理员检查后台配置。")
        return
    except Exception as e:
        # 内部异常不回给用户，里面有 SQL 和文件路径
        logger.exception(f"u2a fatal uid={uid}: {e}")
        await safe_reply(update, "发送失败，请稍后重试。")
        return

    with db_session() as s:
        s.add(
            MessageMap(
                user_chat_message_id=msg.id,
                group_chat_message_id=sent.message_id,
                user_id=uid,
            )
        )


async def forwarding_message_a2u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    thread_id = msg.message_thread_id
    if not thread_id:
        return

    user_id = 0
    with db_session() as s:
        u = s.query(User).filter(User.message_thread_id == thread_id).first()
        if u:
            user_id = u.user_id
    if not user_id:
        return

    if msg.forum_topic_created:
        with db_session() as s:
            s.add(FormnStatus(message_thread_id=thread_id, status="opened"))
        return

    if msg.forum_topic_closed:
        try:
            await context.bot.send_message(user_id, "对话已关闭，消息暂时无法送达。")
        except Exception as e:
            logger.debug(f"notify closed failed: {e}")
        with db_session() as s:
            f = s.query(FormnStatus).filter(FormnStatus.message_thread_id == thread_id).first()
            if f:
                f.status = "closed"
            else:
                s.add(FormnStatus(message_thread_id=thread_id, status="closed"))
        return

    if msg.forum_topic_reopened:
        try:
            await context.bot.send_message(user_id, "对话已重新打开，可以继续发送消息了。")
        except Exception as e:
            logger.debug(f"notify reopened failed: {e}")
        with db_session() as s:
            f = s.query(FormnStatus).filter(FormnStatus.message_thread_id == thread_id).first()
            if f:
                f.status = "opened"
            else:
                s.add(FormnStatus(message_thread_id=thread_id, status="opened"))
        return

    with db_session() as s:
        f = s.query(FormnStatus).filter(FormnStatus.message_thread_id == thread_id).first()
        if f and f.status == "closed":
            await safe_reply(update, "对话已关闭，请先打开话题。")
            return

    # 用户已 /stop：不推送，但要回执，否则管理员以为发出去了
    if _is_stopped(user_id):
        await safe_reply(
            update,
            f"该用户已发送 /stop 停止接收（{user_id}），这条没有送达。",
            message_thread_id=thread_id,
        )
        return

    params = {}
    if msg.reply_to_message:
        with db_session() as s:
            m = (
                s.query(MessageMap)
                .filter(MessageMap.group_chat_message_id == msg.reply_to_message.message_id)
                .first()
            )
            if m:
                params["reply_to_message_id"] = m.user_chat_message_id
        params["allow_sending_without_reply"] = True

    if msg.media_group_id:
        media_type, file_id = _album_item(msg)
        with db_session() as s:
            s.add(
                MediaGroupMesssage(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    media_group_id=msg.media_group_id,
                    is_header=False,
                    caption_html=msg.caption_html,
                    media_type=media_type,
                    file_id=file_id,
                )
            )
        await send_media_group_later(
            _MEDIA_GROUP_DELAY, msg.chat.id, user_id, msg.media_group_id, "a2u", context
        )
        return

    try:
        sent = await _retry(
            lambda: context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=update.effective_chat.id,
                message_id=msg.id,
                **params,
            )
        )
    except Forbidden:
        # 100% 确定：用户把 bot 拉黑了 / 注销了账号。重试没有意义。
        logger.info(f"a2u forbidden: user {user_id} blocked the bot")
        await safe_reply(update, "对方已经拉黑了机器人，这条消息未送达。")
        return
    except BadRequest as e:
        logger.warning(f"a2u send failed to {user_id}: {e}")
        await safe_reply(update, "这条消息发不出去，可能是原消息已被删除。")
        return
    except Exception as e:
        logger.warning(f"a2u send failed to {user_id}: {e}")
        await safe_reply(update, "发送超时，对方可能没收到，建议重发一次。")
        return

    with db_session() as s:
        s.add(
            MessageMap(
                group_chat_message_id=msg.id,
                user_chat_message_id=sent.message_id,
                user_id=user_id,
            )
        )


async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户改了消息之后，后台看到的还是旧内容。"""
    msg = update.edited_message
    if not msg:
        return
    user = update.effective_user
    if _is_stopped(user.id):
        return

    with db_session() as s:
        u = s.query(User).filter(User.user_id == user.id).first()
        thread_id = u.message_thread_id if u else 0
    if not thread_id:
        return

    text = msg.text or msg.caption or ""
    if not text:
        return

    verdict = spam_filter.check(
        MsgInfo(
            user_id=user.id,
            text=text,
            name=user.full_name,
            username=user.username or "",
            is_new_user=False,
        )
    )
    if verdict.block:
        logger.info(f"edited message blocked uid={user.id} {verdict.reasons}")
        return

    try:
        await context.bot.send_message(
            admin_group_id,
            f"✏️ 对方修改了消息：\n「{preview_line(text, 200)}」",
            message_thread_id=thread_id,
        )
    except Exception as e:
        logger.debug(f"edited notify failed: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """更新处理链的最外层兜底。"""
    err = context.error

    # 不能 isinstance(err, NetworkError)：BadRequest 也是子类
    if _is_retryable(err):
        logger.warning(f"网络异常（可自愈）：{type(err).__name__}: {err}")
        return

    if isinstance(err, RetryAfter):
        logger.warning(f"被 Telegram 限速：{err.retry_after} 秒")
        return

    logger.error(
        f"Exception while handling an update: {type(err).__name__}: {err}",
        exc_info=err,
    )

    # 限频通知管理员
    now = time.time()
    last = context.bot_data.get("last_error_alert", 0)
    if now - last < 300:
        return
    context.bot_data["last_error_alert"] = now

    try:
        await context.bot.send_message(
            admin_group_id,
            f"⚠️ 机器人出现异常\n<code>{type(err).__name__}: "
            f"{str(err)[:300]}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# 命令菜单：管理员那份只挂后台群
_USER_COMMANDS = [
    BotCommand("start", "开始 / 恢复接收"),
    BotCommand("help", "使用说明"),
    BotCommand("privacy", "我保存了你哪些信息"),
    BotCommand("stop", "停止接收消息"),
]

_ADMIN_GROUP_COMMANDS = _USER_COMMANDS + [
    BotCommand("clear", "在话题里发送：关闭会话并删除话题"),
    BotCommand("broadcast", "回复一条消息发送：广播给全部用户"),
    BotCommand("release_user", "放行用户：/release_user 用户ID"),
]


async def post_init(application):
    try:
        await application.bot.set_my_commands(_USER_COMMANDS)
    except Exception as e:
        logger.warning(f"设置用户命令菜单失败: {e}")

    try:
        await application.bot.set_my_commands(
            _ADMIN_GROUP_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_group_id)
        )
    except Exception as e:
        logger.info(f"后台群命令菜单未生效: {e}")


if __name__ == "__main__":
    os.makedirs(ASSETS_DIR, exist_ok=True)

    pickle_persistence = PicklePersistence(
        filepath=os.path.join(ASSETS_DIR, f"{app_name}.pickle")
    )
    application = (
        ApplicationBuilder()
        .token(bot_token)
        .persistence(persistence=pickle_persistence)
        # 并发前提：per-operation Session + 每用户锁
        .concurrent_updates(True)
        # 放宽超时：跨境线路按默认 5 秒太紧
        .connect_timeout(15.0)
        .read_timeout(15.0)
        .write_timeout(30.0)
        .media_write_timeout(60.0)
        .pool_timeout(5.0)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    # /help 不限私聊：管理员在话题里也能随手查，回复会跟到同一个话题
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("privacy", privacy_cmd, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("stop", stop_cmd, filters.ChatType.PRIVATE))
    application.add_handler(
        MessageHandler(~filters.COMMAND & filters.ChatType.PRIVATE, forwarding_message_u2a)
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & filters.ChatType.PRIVATE,
            on_edited_message,
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.Chat([admin_group_id]), forwarding_message_a2u
        )
    )
    for cmd, fn in (("clear", clear), ("broadcast", broadcast), ("release_user", release_user)):
        application.add_handler(CommandHandler(cmd, fn, filters.Chat([admin_group_id])))

    application.add_error_handler(error_handler)

    application.job_queue.run_daily(
        daily_spam_report, time=dtime(9, 0, tzinfo=TZ), name="daily_spam_report"
    )
    application.job_queue.run_daily(daily_prune, time=dtime(4, 0, tzinfo=TZ), name="daily_prune")

    application.run_polling()
