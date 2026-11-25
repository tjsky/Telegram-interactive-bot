"""人机验证引擎：图片渲染、挑战存储、配额与冷却。不依赖 telegram。"""

from __future__ import annotations

import io
import math
import random
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - 部署环境问题，不该进单测
    # Pillow 缺失时直接给修复指引，不打裸 traceback
    import sys

    raise SystemExit(
        "\n"
        "✗ 缺少 Pillow，验证码图片无法生成，bot 拒绝启动。\n"
        f"  当前解释器：{sys.executable}\n"
        f"  Python 版本：{sys.version.split()[0]}\n"
        "\n"
        "  修复（用上面这个解释器执行）：\n"
        f"    {sys.executable} -m pip install -r requirements.txt\n"
        "  只想补这一个包：\n"
        f"    {sys.executable} -m pip install 'Pillow>=10.4'\n"
    ) from None


@dataclass
class CaptchaConfig:
    # 易混字符（0/O/1/I/L）已剔除；答案与干扰项必须同源
    charset: str = "23456789ACDEFHJKMNPQRUVWXY"
    # 长度 6：答案空间十亿级，蜜罐撞车的概率才够低
    length: int = 6

    width: int = 340
    height: int = 100
    # 留空则用 Pillow 内置字体；建议放 3 种以上风格差异大的
    font_paths: Sequence[str] = ()

    char_rotate: float = 26.0       # 单字旋转角度范围 ±deg
    char_scale: tuple[float, float] = (0.78, 1.05)
    wave_amplitude: float = 6.0     # 整行正弦位移幅度（px）
    noise_ratio: float = 0.05       # 椒盐噪点比例
    curve_count: tuple[int, int] = (2, 4)   # 干扰曲线条数
    blur_radius: float = 0.45

    ttl: float = 180.0              # 挑战有效期（秒）

    # 冷却阶梯（秒），索引 = 已失败次数，超出取最后一档
    cooldown_ladder: tuple[float, ...] = (0.0, 30.0, 120.0, 600.0, 3600.0, 86400.0)
    max_fails: int = 6              # 累计失败达到此值 -> 拉黑

    honeypot_window: float = 7 * 86400.0   # 答案复用检测的时间窗
    honeypot_min_users: int = 2            # 同一答案被 >= N 个账号用过即判定批量破解


class Verdict(Enum):
    OK = "ok"
    WRONG = "wrong"
    EXPIRED = "expired"          # 挑战超时或已被消费
    NO_CHALLENGE = "no_challenge"
    COOLDOWN = "cooldown"        # 还在冷却，不该走到这里（调用方应先拦）
    BANNED = "banned"            # 累计失败过多
    HONEYPOT = "honeypot"        # 通过，但命中批量破解特征 -> 调用方应影子封禁


@dataclass
class VerifyResult:
    verdict: Verdict
    cooldown_left: float = 0.0
    fails: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.OK, Verdict.HONEYPOT)


@dataclass
class Challenge:
    user_id: int
    token: str
    answer: str
    options: list[str]
    image: bytes            # PNG 字节流，直接给 telegram 当 InputFile
    issued_at: float

    @property
    def correct_index(self) -> int:
        return self.options.index(self.answer)


class ChallengeStore:
    """单进程内存 + 可选 SQLite 持久化。"""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS captcha_state (
        user_id     INTEGER PRIMARY KEY,
        fails       INTEGER NOT NULL DEFAULT 0,
        banned      INTEGER NOT NULL DEFAULT 0,
        last_fail   REAL    NOT NULL DEFAULT 0,
        passed_at   REAL    NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS captcha_issued (
        answer   TEXT    NOT NULL,
        user_id  INTEGER NOT NULL,
        ts       REAL    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_issued_answer ON captcha_issued(answer, ts);
    """

    def __init__(self, persist_path: str | None = None):
        self._lock = threading.RLock()
        self._challenges: dict[int, Challenge] = {}
        self._conn: sqlite3.Connection | None = None

        if persist_path:
            self._conn = sqlite3.connect(persist_path, check_same_thread=False)
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()

        self._mem_state: dict[int, dict] = {}
        self._mem_issued: list[tuple[str, int, float]] = []


    def put_challenge(self, ch: Challenge) -> None:
        with self._lock:
            self._challenges[ch.user_id] = ch

    def pop_challenge(self, user_id: int) -> Challenge | None:
        with self._lock:
            return self._challenges.pop(user_id, None)

    def peek_challenge(self, user_id: int) -> Challenge | None:
        with self._lock:
            return self._challenges.get(user_id)

    def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            dead = [u for u, c in self._challenges.items() if now - c.issued_at > 900]
            for u in dead:
                self._challenges.pop(u, None)
            # 已发答案留 24 小时，只服务于蜜罐判断
            if self._conn:
                self._conn.execute("DELETE FROM captcha_issued WHERE ts<?", (now - 86400,))
                self._conn.commit()
            else:
                self._mem_issued = [r for r in self._mem_issued if r[2] >= now - 86400]


    def get_state(self, user_id: int) -> tuple[int, bool, float, float]:
        """返回 (fails, banned, last_fail, passed_at)"""
        with self._lock:
            if self._conn:
                row = self._conn.execute(
                    "SELECT fails, banned, last_fail, passed_at "
                    "FROM captcha_state WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            else:
                s = self._mem_state.get(user_id)
                row = (
                    (s["fails"], s["banned"], s["last_fail"], s["passed_at"])
                    if s
                    else None
                )
        return tuple(row) if row else (0, False, 0.0, 0.0)

    def bump_fail(self, user_id: int, now: float) -> int:
        with self._lock:
            fails, banned, _, passed_at = self.get_state(user_id)
            fails += 1
            if self._conn:
                self._conn.execute(
                    "INSERT INTO captcha_state(user_id, fails, banned, last_fail, passed_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "fails=excluded.fails, banned=excluded.banned, "
                    "last_fail=excluded.last_fail",
                    (user_id, fails, int(banned), now, passed_at),
                )
                self._conn.commit()
            else:
                self._mem_state[user_id] = {
                    "fails": fails,
                    "banned": banned,
                    "last_fail": now,
                    "passed_at": passed_at,
                }
            return fails

    def mark_banned(self, user_id: int) -> None:
        with self._lock:
            fails, _, last_fail, passed_at = self.get_state(user_id)
            if self._conn:
                self._conn.execute(
                    "INSERT INTO captcha_state(user_id, fails, banned, last_fail, passed_at) "
                    "VALUES(?,?,1,?,?) ON CONFLICT(user_id) DO UPDATE SET banned=1",
                    (user_id, fails, last_fail, passed_at),
                )
                self._conn.commit()
            else:
                self._mem_state.setdefault(
                    user_id,
                    {"fails": fails, "banned": False, "last_fail": last_fail, "passed_at": passed_at},
                )["banned"] = True

    def mark_passed(self, user_id: int, now: float) -> None:
        with self._lock:
            fails, banned, last_fail, _ = self.get_state(user_id)
            if self._conn:
                self._conn.execute(
                    "INSERT INTO captcha_state(user_id, fails, banned, last_fail, passed_at) "
                    "VALUES(?,0,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "fails=0, passed_at=excluded.passed_at",
                    (user_id, int(banned), last_fail, now),
                )
                self._conn.commit()
            else:
                self._mem_state[user_id] = {
                    "fails": 0,
                    "banned": banned,
                    "last_fail": last_fail,
                    "passed_at": now,
                }

    def reset(self, user_id: int) -> None:
        """管理员放行时调用：清掉失败计数、封禁标记和当前挑战。"""
        with self._lock:
            self._challenges.pop(user_id, None)
            if self._conn:
                self._conn.execute(
                    "DELETE FROM captcha_state WHERE user_id=?", (user_id,)
                )
                self._conn.commit()
            else:
                self._mem_state.pop(user_id, None)


    def record_issued(self, answer: str, user_id: int, now: float) -> None:
        with self._lock:
            if self._conn:
                self._conn.execute(
                    "INSERT INTO captcha_issued(answer, user_id, ts) VALUES(?,?,?)",
                    (answer, user_id, now),
                )
                self._conn.commit()
            else:
                self._mem_issued.append((answer, user_id, now))
                if len(self._mem_issued) > 50000:
                    cutoff = now - 86400
                    self._mem_issued = [r for r in self._mem_issued if r[2] > cutoff]

    def answer_users(self, answer: str, since: float) -> set[int]:
        """这个答案在时间窗内发给了哪些账号"""
        with self._lock:
            if self._conn:
                rows = self._conn.execute(
                    "SELECT DISTINCT user_id FROM captcha_issued "
                    "WHERE answer=? AND ts>=?",
                    (answer, since),
                ).fetchall()
                return {r[0] for r in rows}
            return {u for a, u, t in self._mem_issued if a == answer and t >= since}

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class CaptchaRenderer:
    """自己画，不用 captcha 库。"""

    def __init__(self, cfg: CaptchaConfig):
        self.cfg = cfg
        self._fonts: list[str] = [p for p in cfg.font_paths if p]

    def _pick_font(self, size: int) -> ImageFont.FreeTypeFont:
        if self._fonts:
            try:
                return ImageFont.truetype(random.choice(self._fonts), size)
            except OSError:
                pass
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()

    def _text_size(self, draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]

    def render(self, text: str) -> bytes:
        cfg = self.cfg
        w, h = cfg.width, cfg.height

        # 浅色渐变 + 细噪点，别让 OCR 二值化太干净
        bg = Image.new("RGB", (w, h), (250, 250, 250))
        d = ImageDraw.Draw(bg)
        for y in range(h):
            shade = 246 + int(6 * math.sin(y / h * math.pi))
            d.line([(0, y), (w, y)], fill=(shade, shade, shade))

        for _ in range(random.randint(*cfg.curve_count)):
            pts = [
                (
                    random.randint(-10, w + 10),
                    random.randint(0, h),
                )
                for _ in range(4)
            ]
            col = random.randint(150, 205)
            d.line(pts, fill=(col, col, col), width=random.choice([1, 1, 2]), joint="curve")

        # 逐字画到独立图层，便于单独旋转
        n = len(text)
        size = int(min(h * 0.68, (w * 0.82) / max(n, 1) / 0.62))
        size = max(size, 18)

        char_imgs: list[tuple[Image.Image, int, int]] = []
        for ch in text:
            font = self._pick_font(int(size * random.uniform(*cfg.char_scale)))
            tmp = Image.new("RGBA", (size * 3, size * 3), (0, 0, 0, 0))
            td = ImageDraw.Draw(tmp)
            td.text((size, size), ch, font=font, fill=self._char_color())
            bbox = tmp.getbbox()
            if not bbox:
                continue
            glyph = tmp.crop(bbox)
            angle = random.uniform(-cfg.char_rotate, cfg.char_rotate)
            glyph = glyph.rotate(angle, resample=Image.BICUBIC, expand=True)
            char_imgs.append((glyph, 0, 0))

        if not char_imgs:
            return self._encode(bg)

        # 先扣最小字距再分配宽度，否则宽字体下相邻字符会贴成一团
        avail = w * 0.90
        n_gap = max(len(char_imgs) - 1, 1)
        gap_min = 6

        budget = avail - gap_min * n_gap
        total = sum(g.width for g, _, _ in char_imgs)
        if total > budget:
            ratio = budget / total
            char_imgs = [
                (
                    g.resize(
                        (max(1, int(g.width * ratio)), max(1, int(g.height * ratio))),
                        Image.BICUBIC,
                    ),
                    a,
                    b,
                )
                for g, a, b in char_imgs
            ]

        widths = [g.width for g, _, _ in char_imgs]
        total = sum(widths)
        gap = max(gap_min, int((avail - total) / n_gap))
        x = int((w - (total + gap * n_gap)) / 2)

        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for (glyph, _, _), gw in zip(char_imgs, widths):
            y = int((h - glyph.height) / 2 + random.uniform(-h * 0.09, h * 0.09))
            x = min(max(0, x), w - gw)
            layer.alpha_composite(glyph, (x, max(0, y)))
            # 只允许 1px 前移，保留字距
            x += gw + gap + random.randint(-1, 1)

        bg = Image.alpha_composite(bg.convert("RGBA"), layer).convert("RGB")

        # 整行正弦位移，破坏 OCR 的水平投影切分
        if cfg.wave_amplitude > 0:
            bg = self._wave(bg, cfg.wave_amplitude)

        # 只撒深色噪点，浅色会被当成背景
        if cfg.noise_ratio > 0:
            d = ImageDraw.Draw(bg)
            for _ in range(int(w * h * cfg.noise_ratio)):
                px, py = random.randrange(w), random.randrange(h)
                g = random.randint(40, 120)
                d.point((px, py), fill=(g, g, g))

        if cfg.blur_radius > 0:
            bg = bg.filter(ImageFilter.GaussianBlur(cfg.blur_radius))

        return self._encode(bg)

    @staticmethod
    def _char_color() -> tuple[int, int, int]:
        # 深色但随机色相，避免二值化后字形过于完整
        return (
            random.randint(10, 90),
            random.randint(10, 90),
            random.randint(10, 90),
        )

    @staticmethod
    def _wave(img: Image.Image, amp: float) -> Image.Image:
        w, h = img.size
        out = Image.new("RGB", (w, h), (250, 250, 250))
        freq = random.uniform(0.05, 0.11)
        phase = random.uniform(0, math.tau)
        for y in range(h):
            dx = int(amp * math.sin(y * freq + phase))
            row = img.crop((0, y, w, y + 1))
            out.paste(row, (dx, y))
        return out

    @staticmethod
    def _encode(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


class CaptchaEngine:
    def __init__(
        self,
        config: CaptchaConfig | None = None,
        store: ChallengeStore | None = None,
    ):
        self.cfg = config or CaptchaConfig()
        self.store = store or ChallengeStore()
        self.renderer = CaptchaRenderer(self.cfg)


    def _make_options(self, answer: str) -> list[str]:
        """构造 7 个干扰项 + 1 个答案，并打乱。"""
        cs = self.cfg.charset
        n = self.cfg.length
        seen = {answer}
        distractors: list[str] = []
        guard = 0

        while len(distractors) < 7 and guard < 5000:
            guard += 1
            cand = "".join(secrets.choice(cs) for _ in range(n))
            if cand in seen:
                continue
            if _hamming(cand, answer) < 2:
                continue
            if any(_hamming(cand, d) < 2 for d in distractors):
                continue
            seen.add(cand)
            distractors.append(cand)

        # 字符集过小时兜底，保证 8 个选项
        while len(distractors) < 7:
            cand = "".join(secrets.choice(cs) for _ in range(n))
            if cand not in seen:
                seen.add(cand)
                distractors.append(cand)

        options = distractors + [answer]
        secrets.SystemRandom().shuffle(options)
        return options


    def cooldown_left(self, user_id: int) -> float:
        fails, banned, last_fail, _ = self.store.get_state(user_id)
        if banned or fails >= self.cfg.max_fails:
            return -1.0  # 永久
        if fails == 0:
            return 0.0
        ladder = self.cfg.cooldown_ladder
        wait = ladder[min(fails, len(ladder) - 1)]
        return max(0.0, last_fail + wait - time.time())

    def issue(self, user_id: int) -> Challenge:
        now = time.time()
        answer = "".join(secrets.choice(self.cfg.charset) for _ in range(self.cfg.length))
        options = self._make_options(answer)
        png = self.renderer.render(answer)
        token = secrets.token_hex(6)

        ch = Challenge(
            user_id=user_id,
            token=token,
            answer=answer,
            options=options,
            image=png,
            issued_at=now,
        )
        self.store.put_challenge(ch)
        self.store.record_issued(answer, user_id, now)
        return ch

    def verify(self, user_id: int, token: str, index: int) -> VerifyResult:
        """按钮模式：用户点了第 index 个选项。"""
        now = time.time()
        ch, early = self._precheck(user_id, now)
        if early:
            return early
        if not secrets.compare_digest(ch.token, token):
            return VerifyResult(Verdict.NO_CHALLENGE, detail="token mismatch")

        self.store.pop_challenge(user_id)
        try:
            picked = ch.options[index]
        except IndexError:
            picked = ""
        return self._settle(user_id, ch, picked, now)

    def verify_text(self, user_id: int, text: str) -> VerifyResult:
        """打字模式：用户直接输入图片里的字符。"""
        now = time.time()
        ch, early = self._precheck(user_id, now)
        if early:
            return early

        self.store.pop_challenge(user_id)
        norm = "".join(c for c in text.upper() if c in self.cfg.charset)
        return self._settle(user_id, ch, norm, now)


    def _precheck(self, user_id: int, now: float):
        """公共前置检查。返回 (challenge, 提前结束的结果或 None)。"""
        fails, banned, _, _ = self.store.get_state(user_id)
        if banned or fails >= self.cfg.max_fails:
            return None, VerifyResult(Verdict.BANNED, fails=fails)

        ch = self.store.peek_challenge(user_id)
        if ch is None:
            return None, VerifyResult(Verdict.NO_CHALLENGE, fails=fails)

        if now - ch.issued_at > self.cfg.ttl:
            self.store.pop_challenge(user_id)
            return None, VerifyResult(Verdict.EXPIRED, fails=fails)

        return ch, None

    def _settle(self, user_id: int, ch: Challenge, picked: str, now: float) -> VerifyResult:
        fails, _, _, _ = self.store.get_state(user_id)

        if picked != ch.answer:
            if picked:
                others = self.store.answer_users(picked, now - self.cfg.honeypot_window)
                others.discard(user_id)
                if others:
                    self.store.mark_banned(user_id)
                    return VerifyResult(
                        Verdict.BANNED,
                        fails=fails + 1,
                        detail=f"guess hit another user's answer ({len(others)} users)",
                    )

            fails = self.store.bump_fail(user_id, now)
            if fails >= self.cfg.max_fails:
                self.store.mark_banned(user_id)
                return VerifyResult(Verdict.BANNED, fails=fails)
            ladder = self.cfg.cooldown_ladder
            return VerifyResult(
                Verdict.WRONG,
                cooldown_left=ladder[min(fails, len(ladder) - 1)],
                fails=fails,
            )

        self.store.mark_passed(user_id, now)
        users = self.store.answer_users(ch.answer, now - self.cfg.honeypot_window)
        if len(users) >= self.cfg.honeypot_min_users:
            return VerifyResult(
                Verdict.HONEYPOT,
                fails=fails,
                detail=f"answer shared by {len(users)} accounts",
            )
        return VerifyResult(Verdict.OK, fails=0)

    def is_human(self, user_id: int) -> bool:
        """是否已通过验证，且通过时间在 allow_after_pass 之内（默认永久）。"""
        _, banned, _, passed_at = self.store.get_state(user_id)
        return bool(passed_at) and not banned


def _hamming(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


# 通过后的配额，第二道防线


class QuotaGuard:
    """验证码只证明"此刻不是脚本"。"""

    def __init__(
        self,
        window: float = 600.0,
        max_messages: int = 15,
        new_user_window: float = 3600.0,
        new_user_max: int = 5,
    ):
        self.window = window
        self.max_messages = max_messages
        self.new_user_window = new_user_window
        self.new_user_max = new_user_max
        self._hits: dict[int, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: int, passed_at: float) -> tuple[bool, str]:
        now = time.time()
        with self._lock:
            bucket = [t for t in self._hits.get(user_id, []) if now - t < self.window]
            limit = (
                self.new_user_max
                if now - passed_at < self.new_user_window
                else self.max_messages
            )
            if len(bucket) >= limit:
                self._hits[user_id] = bucket
                return False, f"发送过于频繁，请 {int(self.window / 60)} 分钟后再试。"
            bucket.append(now)
            self._hits[user_id] = bucket
        return True, ""


if __name__ == "__main__":
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    font_dir = os.path.join(here, "..", "fonts")
    fonts = sorted(
        os.path.join(font_dir, f)
        for f in os.listdir(font_dir)
        if f.lower().endswith((".ttf", ".otf"))
    ) if os.path.isdir(font_dir) else []

    cfg = CaptchaConfig(font_paths=fonts)
    engine = CaptchaEngine(cfg)

    out = os.path.join(here, "preview")
    os.makedirs(out, exist_ok=True)

    grid = Image.new("RGB", (cfg.width * 2 + 30, (cfg.height + 10) * 6 + 10), (255, 255, 255))
    for i in range(12):
        ch = engine.issue(user_id=1000 + i)
        img = Image.open(io.BytesIO(ch.image))
        gx = 10 + (i % 2) * (cfg.width + 10)
        gy = 10 + (i // 2) * (cfg.height + 10)
        grid.paste(img, (gx, gy))
        img.save(os.path.join(out, f"{i:02d}_{ch.answer}.png"))
        print(f"{i:02d}  答案={ch.answer}  选项={ch.options}")
    grid.save(os.path.join(out, "_grid.png"))
    print(f"\n预览已写入 {out}")

    # 正确性自检
    ch = engine.issue(user_id=1)
    r = engine.verify(1, ch.token, ch.correct_index)
    assert r.verdict is Verdict.OK, r
    print("通过校验 OK")

    # 打字模式
    ch = engine.issue(user_id=3)
    r = engine.verify_text(3, f" {ch.answer.lower()} ")
    assert r.verdict is Verdict.OK, r
    print("打字模式 OK（大小写/空格自动归一）")

    ch = engine.issue(user_id=4)
    r = engine.verify_text(4, "XXXXX")
    assert r.verdict is Verdict.WRONG, r
    print(f"打字模式错误 OK，冷却 {r.cooldown_left:.0f}s")

    ch = engine.issue(user_id=2)
    wrong = (ch.correct_index + 1) % len(ch.options)
    r = engine.verify(2, ch.token, wrong)
    assert r.verdict is Verdict.WRONG, r
    print(f"错误校验 OK，冷却 {r.cooldown_left:.0f}s")

    # 重放
    r = engine.verify(2, ch.token, ch.correct_index)
    assert r.verdict is Verdict.NO_CHALLENGE, r
    print("重放拦截 OK")

    # 选项泄露检查
    for _ in range(300):
        c = engine.issue(user_id=9000)
        assert len(c.options) == 8
        assert len({o for o in c.options}) == 8
        assert all(len(o) == cfg.length for o in c.options)
        assert all(set(o) <= set(cfg.charset) for o in c.options)
        assert all(_hamming(o, c.answer) >= 2 for o in c.options if o != c.answer)
    print("选项约束 OK（8 项 / 同长度 / 同字符集 / 编辑距离>=2）")
