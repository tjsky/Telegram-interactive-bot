"""私聊垃圾过滤：归一化 → SimHash/LSH 跨账号同文 → 关键词辅助，外加每日汇总。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta


# 零宽 / 双向控制 / 软连字符：最常见的绕过手段
_INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u00ad\u180e\ufeff\u061c]"
)

# URL：http(s)、裸域名、t.me、@username
_URL = re.compile(
    r"""(?ix)
    (?:
        (?:https?://|www\.)[^\s<>"'）】]+
      | t\.me/[^\s<>"'）】]+
      | (?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|cn|net|org|io|me|cc|top|xyz|vip|link|shop|site|info|app|tv|co|ru|tk|ml|ga|cf|gq)
        (?:/[^\s<>"'）】]*)?
    )
    """
)

_DOMAIN = re.compile(r"(?i)(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)")

# emoji / 装饰符号（保留 CJK 与常规标点）
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\uFE0F\u20E3]"
)

_CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")

# 常见"插入分隔符"绕过：微*信 / 微-信 / 微·信
_SEP_BETWEEN = re.compile(r"(?<=[\u4e00-\u9fff])[^\w\u4e00-\u9fff]+(?=[\u4e00-\u9fff])")

_MD = re.compile(r"[*_~`>#|\[\]()]")


def strip_links(text: str) -> str:
    return _URL.sub(" ", text or "")


def normalize(text: str) -> str:
    """归一化到"同一个广告得到同一个字符串"的程度。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _INVISIBLE.sub("", t)
    t = _EMOJI.sub("", t)

    # 链接 -> 统一占位符（保留域名，域名本身是强信号）
    def _sub_url(m: re.Match) -> str:
        dm = _DOMAIN.search(m.group(0))
        return f" \x01{dm.group(1).lower() if dm else 'url'}\x01 "

    t = _URL.sub(_sub_url, t)
    t = t.lower()
    t = _SEP_BETWEEN.sub("", t)      # 微*信 -> 微信
    t = _CJK_GAP.sub("", t)          # 微 信 -> 微信
    t = re.sub(r"[^\w\x01\u4e00-\u9fff]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def preview_line(text: str, limit: int = 60) -> str:
    """给后台看的预览：超链替换成 [域名]、去 markdown、去 emoji，取第一行。"""
    if not text:
        return "（无文本）"

    def _short(m: re.Match) -> str:
        dm = _DOMAIN.search(m.group(0))
        return f"[{dm.group(1).lower() if dm else 'link'}]"

    t = _URL.sub(_short, text)
    t = _EMOJI.sub("", t)
    t = _MD.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    for line in t.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return "（无文本）"


def _tokens(norm_text: str) -> list[str]:
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", norm_text):
        out.append(w)
    for run in _CJK_RUN.findall(norm_text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    for dm in re.findall(r"\x01([^\x01]+)\x01", norm_text):
        out.append("d:" + dm)
    return out


def simhash(norm_text: str) -> int:
    counts: dict[str, int] = {}
    for tok in _tokens(norm_text):
        counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return 0
    v = [0] * 64
    for tok, w in counts.items():
        h = int.from_bytes(
            hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for i in range(64):
            v[i] += w if (h >> i) & 1 else -w
    fp = 0
    for i in range(64):
        if v[i] > 0:
            fp |= 1 << i
    return fp


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _bands(fp: int) -> tuple[int, int, int, int]:
    return (fp & 0xFFFF, (fp >> 16) & 0xFFFF, (fp >> 32) & 0xFFFF, (fp >> 48) & 0xFFFF)


# simhash 是无符号 64 位，落库前转有符号，读出来再掩回
def _to_db(fp: int) -> int:
    return fp - (1 << 64) if fp >> 63 else fp


def _from_db(x: int) -> int:
    return x & 0xFFFFFFFFFFFFFFFF


@dataclass
class FilterConfig:
    block_score: int = 60           # 老用户
    block_score_new: int = 30       # 从没发过消息的账号，标准更严
    review_score: int = 30          # 达到此分记进"待复核"，但仍放行

    near_dup_distance: int = 3      # SimHash Hamming 阈值
    near_dup_window: float = 7 * 86400.0
    near_dup_score: int = 60        # 命中他人同文直接够阈值

    domain_window: float = 86400.0
    domain_users_to_blacklist: int = 3   # 同一域名被 >=N 个不同账号发送

    review_after_blocks: int = 3    # 同一用户当天被拦 N 条 -> 进审核队列

    # 关键词只是辅助分：单个弱词不能独立触发拦截
    strong_keywords: tuple[str, ...] = (
        # 广告话术：出现在正常咨询里的概率极低
        "日赚", "月入", "躺赚", "稳赚", "刷单", "返利", "博彩", "菠菜",
        "棋牌", "裸聊", "约炮", "外围", "办证", "代开", "下款", "涨粉",
        "引流", "解封", "招商加盟", "高额返点",
        # 英文灰产
        "crypto", "usdt", "airdrop", "earn daily", "investment plan",
    )
    weak_keywords: tuple[str, ...] = (
        # 联系方式：正常用户也会提到
        "微信", "weixin", "wx号", "vx", "qq号", "扣扣", "加我", "私聊我",
        # 边缘词
        "兼职", "代理", "推广", "贷款", "发票", "彩票", "同城",
    )
    strong_keyword_score: int = 55      # 单个强词就够阈值
    weak_keyword_score: int = 15        # 单个弱词只是加分
    weak_keyword_score_multi: int = 40  # 两个以上弱词

    # 白名单域名不参与多账号统计，否则 t.me 这类会被误封
    domain_whitelist: tuple[str, ...] = (
        "t.me", "telegram.me", "telegram.org", "telesco.pe",
        "github.com", "youtube.com", "youtu.be", "bilibili.com",
        "qq.com", "weixin.qq.com", "baidu.com", "wikipedia.org",
        "google.com", "bing.com", "microsoft.com", "apple.com",
        "zhihu.com", "weibo.com", "xiaohongshu.com", "douyin.com",
    )

    score_multi_links: int = 25     # >=2 个不同链接
    score_long_text: int = 15       # >500 字
    score_no_username: int = 8      # 无 username（弱信号，中国用户很常见）


class SpamStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS spam_fp (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        fp      INTEGER NOT NULL,
        b0      INTEGER NOT NULL, b1 INTEGER NOT NULL,
        b2      INTEGER NOT NULL, b3 INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        ts      REAL    NOT NULL,
        norm    TEXT    NOT NULL,
        blocked INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_fp_b0 ON spam_fp(b0, ts);
    CREATE INDEX IF NOT EXISTS ix_fp_b1 ON spam_fp(b1, ts);
    CREATE INDEX IF NOT EXISTS ix_fp_b2 ON spam_fp(b2, ts);
    CREATE INDEX IF NOT EXISTS ix_fp_b3 ON spam_fp(b3, ts);

    CREATE TABLE IF NOT EXISTS spam_block (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id  INTEGER NOT NULL,
        name     TEXT,
        username TEXT,
        text     TEXT,
        norm     TEXT,
        score    INTEGER,
        reasons  TEXT,
        strong   INTEGER NOT NULL DEFAULT 0,
        is_new   INTEGER NOT NULL DEFAULT 1,
        ts       REAL NOT NULL,
        released INTEGER NOT NULL DEFAULT 0,
        reviewed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_block_ts ON spam_block(ts);

    CREATE TABLE IF NOT EXISTS spam_blacklist (
        fp   INTEGER PRIMARY KEY,
        ts   REAL NOT NULL,
        note TEXT
    );

    CREATE TABLE IF NOT EXISTS spam_domain (
        domain     TEXT PRIMARY KEY,
        users      TEXT NOT NULL DEFAULT '',   -- 逗号分隔的 user_id
        hits       INTEGER NOT NULL DEFAULT 0,
        first_seen REAL,
        last_seen  REAL,
        blacklist  INTEGER NOT NULL DEFAULT 0
    );
    """

    def __init__(self, path: str = ""):
        # 默认路径锚在项目根，避免工作目录不是项目根时新建一套空库
        if not path:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, "assets", "spam.db")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()


    def add_fp(self, fp: int, user_id: int, ts: float, blocked: bool) -> None:
        """只存 64 位指纹，不存原文。"""
        b0, b1, b2, b3 = _bands(fp)
        with self._lock:
            self._conn.execute(
                "INSERT INTO spam_fp(fp,b0,b1,b2,b3,user_id,ts,norm,blocked) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (_to_db(fp), b0, b1, b2, b3, user_id, ts, "", int(blocked)),
            )
            self._conn.commit()

    def near_dups(self, fp: int, since: float, exclude_user: int) -> list[tuple[int, int]]:
        """返回 [(fp, user_id)]，只取 band 命中的候选，再由调用方算精确 Hamming。"""
        b0, b1, b2, b3 = _bands(fp)
        with self._lock:
            rows = self._conn.execute(
                "SELECT fp, user_id FROM spam_fp "
                "WHERE ts>=? AND user_id<>? AND (b0=? OR b1=? OR b2=? OR b3=?) "
                "LIMIT 500",
                (since, exclude_user, b0, b1, b2, b3),
            ).fetchall()
        return [(_from_db(r[0]), r[1]) for r in rows]


    def is_blacklisted(self, fp: int) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM spam_blacklist WHERE fp=?", (_to_db(fp),)
                ).fetchone()
                is not None
            )

    def blacklist(self, fp: int, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO spam_blacklist(fp, ts, note) VALUES(?,?,?)",
                (_to_db(fp), time.time(), note),
            )
            self._conn.commit()


    def touch_domain(self, domain: str, user_id: int, ts: float) -> int:
        """记录域名，返回当前有多少个不同账号发过它。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT users, hits FROM spam_domain WHERE domain=?", (domain,)
            ).fetchone()
            if row:
                users = set(filter(None, row[0].split(",")))
                users.add(str(user_id))
                self._conn.execute(
                    "UPDATE spam_domain SET users=?, hits=?, last_seen=? WHERE domain=?",
                    (",".join(sorted(users)), row[1] + 1, ts, domain),
                )
            else:
                users = {str(user_id)}
                self._conn.execute(
                    "INSERT INTO spam_domain(domain,users,hits,first_seen,last_seen) "
                    "VALUES(?,?,1,?,?)",
                    (domain, ",".join(sorted(users)), ts, ts),
                )
            self._conn.commit()
            return len(users)

    def domain_blacklisted(self, domain: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT blacklist FROM spam_domain WHERE domain=?", (domain,)
            ).fetchone()
            return bool(row and row[0])

    def blacklist_domain(self, domain: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO spam_domain(domain,users,hits,first_seen,last_seen,blacklist) "
                "VALUES(?,'',0,?,?,1) ON CONFLICT(domain) DO UPDATE SET blacklist=1",
                (domain, time.time(), time.time()),
            )
            self._conn.commit()


    def log_block(
        self,
        user_id: int,
        name: str,
        username: str,
        text: str,
        norm: str,
        score: int,
        reasons: list[str],
        ts: float,
        strong: bool = True,
        is_new: bool = True,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO spam_block"
                "(user_id,name,username,text,norm,score,reasons,strong,is_new,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, name, username, text[:4000], norm, score,
                    "|".join(reasons), int(strong), int(is_new), ts,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def blocks_since(self, since: float, until: float | None = None) -> list[sqlite3.Row]:
        until = until or time.time()
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            return self._conn.execute(
                "SELECT * FROM spam_block WHERE ts>=? AND ts<? AND released=0 "
                "ORDER BY ts",
                (since, until),
            ).fetchall()

    def count_today_blocks(self, user_id: int, since: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM spam_block WHERE user_id=? AND ts>=?",
                (user_id, since),
            ).fetchone()
            return row[0]

    def release(self, block_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE spam_block SET released=1 WHERE id=?", (block_id,)
            )
            self._conn.commit()

    def release_user(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE spam_block SET released=1 WHERE user_id=?", (user_id,)
            )
            self._conn.commit()


    def prune(self, normal_days: int = 30, blocked_days: int = 180) -> int:
        """清理指纹表。不清理的话 spam_fp 会无限增长，最终拖慢 LSH 查询。"""
        now = time.time()
        with self._lock:
            n1 = self._conn.execute(
                "DELETE FROM spam_fp WHERE blocked=0 AND ts<?",
                (now - normal_days * 86400,),
            ).rowcount
            n2 = self._conn.execute(
                "DELETE FROM spam_fp WHERE blocked=1 AND ts<?",
                (now - blocked_days * 86400,),
            ).rowcount
            self._conn.commit()
            return n1 + n2


@dataclass
class MsgInfo:
    user_id: int
    text: str
    name: str = ""
    username: str = ""
    is_new_user: bool = True      # 从没在 User 表里出现过
    has_media: bool = False


@dataclass
class Decision:
    block: bool
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    first_line: str = ""
    review: bool = False          # 放行但需要人工看
    strong: bool = False          # 是否存在强信号（弱关键词不算）
    domains: list[str] = field(default_factory=list)


class SpamFilter:
    def __init__(self, cfg: FilterConfig | None = None, store: SpamStore | None = None):
        self.cfg = cfg or FilterConfig()
        self.store = store or SpamStore()

    def check(self, msg: MsgInfo) -> Decision:
        cfg = self.cfg
        now = time.time()
        raw = msg.text or ""
        norm = normalize(raw)
        d = Decision(block=False, first_line=preview_line(raw))

        if not norm:
            return d  # 纯媒体/纯表情，交给别的手段

        fp = simhash(norm)
        reasons: list[str] = []
        score = 0
        strong = False

        if self.store.is_blacklisted(fp):
            d.block = True
            d.score = 100
            d.strong = True
            d.reasons = ["精确命中已确认垃圾指纹"]
            self._persist(msg, norm, fp, d, now)
            return d

        dup_users: set[int] = set()
        for ofp, ouid in self.store.near_dups(
            fp, now - cfg.near_dup_window, msg.user_id
        ):
            if hamming(fp, ofp) <= cfg.near_dup_distance:
                dup_users.add(ouid)

        if dup_users:
            score += cfg.near_dup_score
            strong = True
            reasons.append(f"跨账号同文×{len(dup_users) + 1}")

        # 强词/弱词分开算，单个弱词只加分
        sh = [k for k in cfg.strong_keywords if k in norm]
        wh = [k for k in cfg.weak_keywords if k in norm]

        if sh:
            score += cfg.strong_keyword_score
            strong = True
            reasons.append("强关键词(" + ",".join(sh[:4]) + ")")

        if len(wh) >= 2:
            score += cfg.weak_keyword_score_multi
            strong = True
            reasons.append("弱关键词×%d(%s)" % (len(wh), ",".join(wh[:4])))
        elif wh:
            score += cfg.weak_keyword_score
            reasons.append("弱关键词(%s)" % wh[0])

        domains = [m.lower() for m in _DOMAIN.findall(raw or "")]
        d.domains = sorted(set(domains))
        bl_domain = False
        for dm in d.domains:
            if dm in cfg.domain_whitelist or any(
                dm.endswith("." + w) for w in cfg.domain_whitelist
            ):
                continue
            users_n = self.store.touch_domain(dm, msg.user_id, now)
            if self.store.domain_blacklisted(dm):
                bl_domain = True
                reasons.append(f"黑名单域名({dm})")
            elif users_n >= cfg.domain_users_to_blacklist:
                self.store.blacklist_domain(dm)
                bl_domain = True
                reasons.append(f"域名被{users_n}个账号共享({dm})")

        if bl_domain:
            score += 60
            strong = True
        elif len(d.domains) >= 2:
            score += cfg.score_multi_links
            strong = True
            reasons.append(f"{len(d.domains)}个链接")

        if len(raw) > 500:
            score += cfg.score_long_text
            reasons.append("超长文本")

        if not msg.username and msg.is_new_user:
            score += cfg.score_no_username
            reasons.append("新账号无username")

        # 双条件：分数够且至少有一个强信号，否则话多的正常用户会被误封
        threshold = cfg.block_score_new if msg.is_new_user else cfg.block_score
        d.score = score
        d.reasons = reasons
        d.strong = strong
        d.block = strong and score >= threshold
        d.review = (not d.block) and (score >= cfg.review_score or (strong and score >= 20))

        # 命中同文广告 => 学进黑名单，后续同文案一律秒拦
        if d.block and dup_users:
            self.store.blacklist(fp, note="cross-user duplicate")

        self._persist(msg, norm, fp, d, now)
        return d

    def _persist(self, msg: MsgInfo, norm: str, fp: int, d: Decision, now: float) -> None:
        # 指纹表只记 fp；被拦的消息才落原文（复核和日报要用）
        self.store.add_fp(fp, msg.user_id, now, d.block)
        if d.block:
            self.store.log_block(
                msg.user_id, msg.name, msg.username,
                msg.text, norm, d.score, d.reasons, now, d.strong, msg.is_new_user,
            )


def build_daily_report(store: SpamStore, day: datetime | None = None, limit: int = 15) -> str:
    """生成昨天的拦截汇总。"""
    day = day or (datetime.now() - timedelta(days=1))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = (day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()

    rows = store.blocks_since(start, end)
    title = day.strftime("%m-%d")

    if not rows:
        return f"🛡 {title} 垃圾信息拦截汇总\n\n昨日无拦截记录。"

    by_user: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)

    total_users = len(by_user)
    lines = [f"🛡 {title} 垃圾信息拦截汇总", f"拦截 {len(rows)} 条 / {total_users} 个账号", ""]

    ordered = sorted(by_user.items(), key=lambda kv: -len(kv[1]))

    lines.append("🚫 已拦截")
    for i, (uid, items) in enumerate(ordered[:limit], 1):
        r0 = items[0]
        uname = f" @{r0['username']}" if r0["username"] else ""
        lines.append(f"{i}. {r0['name'] or '未知'}{uname} ({uid}) — {len(items)} 条")
        lines.append(f"   「{preview_line(r0['text'])}」")
        if r0["reasons"]:
            lines.append(f"   命中：{r0['reasons'].replace('|', ' / ')}")

    if total_users > limit:
        lines.append(f"… 另有 {total_users - limit} 个账号")

    # 待复核：分数不高但被拦的，最可能误封
    def _risky(r) -> bool:
        """老客户被拦、或没有任何高置信信号支撑的拦截，才进复核。"""
        rs = r["reasons"] or ""
        high_conf = any(k in rs for k in ("精确命中", "跨账号同文", "强关键词"))
        return (not r["is_new"]) or (not high_conf)

    suspicious = [r for r in rows if _risky(r)]
    if suspicious:
        lines += ["", "👀 建议复核（分数偏低，可能误封）"]
        seen = set()
        n = 0
        for r in suspicious:
            if r["user_id"] in seen:
                continue
            seen.add(r["user_id"])
            n += 1
            if n > 5:
                break
            uname = f" @{r['username']}" if r["username"] else ""
            lines.append(f"{n}. {r['name'] or '未知'}{uname} ({r['user_id']})")
            lines.append(f"   「{preview_line(r['text'])}」")
            lines.append(f"   /release_user {r['user_id']}  可放行该账号")

    return "\n".join(lines)


if __name__ == "__main__":
    import os
    import tempfile

    tmp = os.path.join(tempfile.gettempdir(), "spam_demo.db")
    for f in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(f):
            os.remove(f)

    store = SpamStore(tmp)
    flt = SpamFilter(store=store)

    def run(desc, uid, text, new=True, username=None):
        uname = username if username is not None else ("" if new else f"u{uid}")
        d = flt.check(MsgInfo(user_id=uid, text=text, name=f"用户{uid}",
                              username=uname, is_new_user=new))
        flag = "BLOCK" if d.block else ("review" if d.review else "pass ")
        print(f"[{flag}] score={d.score:<3} {desc:<12} {d.reasons}")
        return d

    print("--- 变体绕过 ---")
    base = "加微信 abc888 日赚300-500，无经验可做，长期稳定"
    run("原始广告", 1001, base)
    run("改全角", 1002, "加ｗｅｉｘｉｎ　ａｂｃ８８８ 日赚300-500，无经验可做，长期稳定")
    run("插零宽", 1003, "加微\u200b信 abc888 日赚300-500，无经验可做，长期稳定")
    run("加空格", 1004, "加 微 信 abc888 日赚300-500，无经验可做，长期稳定")
    run("换同音字", 1005, "加薇信 abc888 日赚300-500，无经验可做，长期稳定")
    run("换链接", 1006, "看这里 https://t.me/spam999 日赚300-500，无经验可做，长期稳定")

    print("\n--- 正常用户（不能误封） ---")
    run("问微信支付", 2001, "你好，请问你们支持微信支付吗")
    run("问报价", 2002, "我想了解一下企业版的报价，方便的话给我发个方案")
    run("老客户问收入", 2003, "你们这个月入大概多少？我想评估下预算", new=False)

    print("\n--- 域名共享自动封禁 ---")
    for i, uid in enumerate((3001, 3002, 3003), 1):
        run(f"同域名第{i}人", uid, f"详情见 https://abc-promo.xyz/offer{i}")

    print("\n--- 误封兜底：老客户撞上被封域名 ---")
    run("老客户发同域名", 2003, "资料在 https://abc-promo.xyz/doc 你看下", new=False)

    print()
    print(build_daily_report(store, day=datetime.now()))
    print()
    print(f"prune: 清理 {store.prune()} 条指纹")
