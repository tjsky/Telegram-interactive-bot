from sqlalchemy import Boolean, Column, Integer, String, DateTime
from sqlalchemy.sql import func
from .database import Base


class MediaGroupMesssage(Base):
    __tablename__ = "media_group_message"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer)
    message_id = Column(Integer)
    media_group_id = Column(Integer)
    is_header = Column(Boolean)
    caption_html = Column(String(1024 * 64))
    # 重发相册要用 sendMediaGroup 重建，必须带着每个元素的 file_id 和类型。
    # 只存 message_id 的话只能 copyMessages，而它是"逐条复制"，相册会散成 N 条。
    file_id = Column(String(512))
    media_type = Column(String(16))


class FormnStatus(Base):
    __tablename__ = "formn_status"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer)
    message_thread_id = Column(Integer)
    status = Column(String(64))


class MessageMap(Base):
    __tablename__ = "message_map"
    id = Column(Integer, primary_key=True, index=True)
    user_chat_message_id = Column(Integer)
    group_chat_message_id = Column(Integer)
    user_id = Column(Integer)


class User(Base):
    __tablename__ = "user"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True)
    first_name = Column(String(64))
    last_name = Column(String(64))
    username = Column(String(64))
    is_premium = Column(Boolean)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    message_thread_id = Column(Integer, default=0)


class UserPref(Base):
    """用户偏好（目前只有 /stop）。不能塞进 user 表：那会把新号洗成老号，放宽垃圾过滤阈值。"""

    __tablename__ = "user_pref"
    user_id = Column(Integer, primary_key=True)
    stopped = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
