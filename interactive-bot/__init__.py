import logging
import logging.handlers
import os
import platform

from dotenv import load_dotenv

# 所有运行时路径都锚在项目根，不依赖启动时的工作目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

# 日志轮转：保留 3 份 × 10MB
_log_file = os.path.join(BASE_DIR, "log.txt")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# apscheduler 的 INFO 会埋掉真告警，只留 WARNING
logging.getLogger("apscheduler").setLevel(logging.WARNING)
current_package = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(current_package)


def _require(key: str) -> str:
    value = (os.getenv(key) or "").strip()
    if not value:
        raise SystemExit(f"{key} 未填写，请检查 .env")
    return value


def _int_env(key: str, default: int | None = None) -> int:
    """环境变量为空字符串时不能用 int() 直接硬转，否则启动就崩。"""
    raw = (os.getenv(key) or "").strip()
    if not raw:
        if default is None:
            raise SystemExit(f"{key} 未填写，请检查 .env")
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{key} 应该是数字，当前是 {raw!r}")


load_dotenv()

bot_token = _require("BOT_TOKEN")
app_name = _require("APP_NAME")
welcome_message = (os.getenv("WELCOME_MESSAGE") or "").strip() or "你好，请直接发送你要说的内容。"

admin_group_id = _int_env("ADMIN_GROUP_ID")

_raw_admins = (os.getenv("ADMIN_USER_IDS") or "").replace("，", ",")
admin_user_ids = [int(x.strip()) for x in _raw_admins.split(",") if x.strip()]
if not admin_user_ids:
    raise SystemExit("ADMIN_USER_IDS 未填写，应为以逗号分隔的数字")

is_delete_topic_as_ban_forever = os.getenv("DELETE_TOPIC_AS_FOREVER_BAN") == "TRUE"
is_delete_user_messages = os.getenv("DELETE_USER_MESSAGE_ON_CLEAR_CMD") == "TRUE"
disable_captcha = os.getenv("DISABLE_CAPTCHA") == "TRUE"
# 0 表示不限制
message_interval = _int_env("MESSAGE_INTERVAL", 5)

# 启动横幅：带 pid 的分隔线切开每次启动，配置行里带上生效的管理员 ID
logger.info("=" * 68)
logger.info(
    "启动 %s | pid=%s | Python %s | %s",
    app_name,
    os.getpid(),
    platform.python_version(),
    BASE_DIR,
)
logger.info(
    "配置：验证码=%s | 间隔=%ss | 后台群=%s | 管理员 %d 人 %s",
    "关闭" if disable_captcha else "开启",
    message_interval,
    admin_group_id,
    len(admin_user_ids),
    ",".join(str(x) for x in admin_user_ids),
)
