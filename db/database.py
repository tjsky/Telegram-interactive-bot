import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 路径锚在项目根，不依赖启动时的工作目录
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_BASE_DIR, "assets", "db.sqlite3").replace("\\", "/")
os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)

SQLALCHEMY_DATABASE_URL = f"sqlite:///{_DB_PATH}"

# SQLite 连接池开大没意义，timeout 让写操作排队
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    connect_args={"timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """WAL 是并发读写的前提。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


# expire_on_commit=False，否则出块后访问属性会抛 DetachedInstanceError
SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)

Base = declarative_base()


# 索引走裸 SQL：create_all 见到表已存在就整张跳过
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_mmap_user ON message_map(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_mmap_group_msg ON message_map(group_chat_message_id)",
    "CREATE INDEX IF NOT EXISTS ix_mmap_user_msg ON message_map(user_chat_message_id)",
    "CREATE INDEX IF NOT EXISTS ix_user_thread ON user(message_thread_id)",
    "CREATE INDEX IF NOT EXISTS ix_formn_thread ON formn_status(message_thread_id)",
    "CREATE INDEX IF NOT EXISTS ix_mg_group ON media_group_message(media_group_id)",
)


def ensure_indexes():
    with engine.begin() as conn:
        for stmt in _INDEXES:
            conn.execute(text(stmt))


# 老库加列必须 ALTER TABLE，create_all 不会补列
_COLUMNS = (
    ("media_group_message", "file_id", "VARCHAR(512)"),
    ("media_group_message", "media_type", "VARCHAR(16)"),
)


def ensure_schema():
    with engine.begin() as conn:
        for table, column, ddl in _COLUMNS:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue  # 表还没建，create_all 会带上新列
            if column not in {r[1] for r in rows}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
