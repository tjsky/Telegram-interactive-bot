# 部署指南

从零到跑起来大约 15 分钟。不需要公网 IP、不需要域名、不需要证书，只要机器能访问 Telegram 即可。

请按顺序做，不要跳步。

---

## 1. 准备 Telegram 侧（最容易翻车的地方）

### 1.1 创建 Bot

1. Telegram 里搜索 **@BotFather**，发送 `/newbot`
2. 给它起一个显示名（随意）和一个用户名（必须以 `bot` 结尾，例如 `my_private_inbox_bot`）
3. 记下返回的 **token**，形如 `7123456789:AAH...`，这就是 `BOT_TOKEN`

> token 等于密码。任何拿到它的人都能完全接管这个 bot。不要提交到 git，不要发群里。

### 1.2 关闭 Privacy Mode（必做）

**这一步漏了，后台群里回复的消息不会转发给用户**，表现为"后台看得到用户消息，回复却发不出去"。

1. 对 @BotFather 发送 `/setprivacy`
2. 选择刚创建的 bot
3. 选 **Disable**

### 1.3 建后台群并开启话题

1. 新建一个群，把 **1.1 的 bot 拉进群**
2. 群设置 → **话题（Topics）→ 开启**

   > 开启话题会让普通群升级成超级群，群 ID 会从 `-1234...` 变成 `-1001234...`。
   > 所以**必须先开话题、再获取 ID。**
3. 把 bot 设为**管理员**，至少勾选：
   - **管理话题**（创建/删除会话话题，必需）
   - **删除消息**（`/clear` 用，建议）

只勾这两个就够，不需要给"添加管理员""封禁用户"这类权限。

### 1.4 获取两个 ID

**后台群 ID（`ADMIN_GROUP_ID`）** —— 在群里发一条消息，然后任选一种：

- 群里 @GetTheirIDBot，或把那条消息转发给它
- 浏览器打开 `https://api.telegram.org/bot<你的TOKEN>/getUpdates`，找 `"chat":{"id":-100...}`

拿到的是 `-100` 开头的负数，**带负号一起填**。
如果返回值里没有这条群消息，说明 bot 没收到 —— 回到 1.2 检查 privacy mode。

**你自己的 user id（`ADMIN_USER_IDS`）** —— 私聊 @userinfobot，它返回的 `Id` 就是。正数。

多个管理员用**英文逗号**分隔：`ADMIN_USER_IDS=1281631753,1022318030`。

---

## 2. 准备机器

### 2.1 Python 版本

**要求 Python 3.10 或以上**（代码用了 `int | None` 这类 3.10 语法）。

检查Python版本：

```bash
python --version
```

3.10 / 3.11 / 3.12 / 3.13 都可以

### 2.2 放置项目

整个 `Telegram-interactive-bot/` 目录拷到目标机器，路径随意。目录里必须有：

```
interactive-bot/    代码
db/                 数据库模型
fonts/              验证码字体
assets/             运行时数据
requirements.txt
.env_example        配置模板
dockerfile
```

`fonts/` 不能删也不能挪走，四个字体是验证码可读性的基础。字体许可证一并保留（SIL OFL 要求）。

### 2.3 装依赖

**Linux / macOS：**

```bash
cd Telegram-interactive-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows（PowerShell 或 CMD）：**

```powershell
cd Telegram-interactive-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

装完确认版本：

```bash
python -c "import telegram, PIL; print(telegram.__version__, PIL.__version__)"
# 应输出 21.11.1 10.x，或者更高的版本，不是的话回到上面重装
```

> **注意 Python 解释器要一致。** `pip install` 和 `python -m interactive-bot`
> 必须来自同一个解释器。常见翻车是：系统 python 装过依赖，实际用 venv 启动，
> 结果启动时缺包。用 venv 时先 `source .venv/bin/activate` 再装再启，
> 或者干脆全程用 `.venv/bin/python -m pip` / `.venv/bin/python -m interactive-bot`。

---

## 3. 写配置

复制模板：

```bash
cp .env_example .env      # Windows: copy .env_example .env
```

编辑 `.env`。**必填五项**：

```ini
APP_NAME=interactive-bot
BOT_TOKEN=7123456789:AAH...            # 1.1 拿到的
ADMIN_GROUP_ID=-1002234110823          # 1.4 拿到的，带负号
ADMIN_USER_IDS=1281631753              # 1.4 拿到的，你自己的
BOT_TIMEZONE=Asia/Shanghai
```

其余保持默认即可，含义如下：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `APP_NAME` | `interactive-bot` | 也用作 pickle 文件名，改了等于全新启动 |
| `WELCOME_MESSAGE` | 你好，请直接发送你要说的内容。 | 用户 `/start` 时和第一次和Bot对话时，所看到的第一句话 |
| `BOT_TIMEZONE` | `Asia/Shanghai` | 每日定时任务的时间采用这个时区 |
| `DELETE_TOPIC_AS_FOREVER_BAN` | `FALSE` | `TRUE` 时删除话题 = 永久封禁该用户，不可恢复 |
| `DELETE_USER_MESSAGE_ON_CLEAR_CMD` | `TRUE` | `/clear` 时连用户那边的消息一起删（以防你发了不该发的东西，但这是个君子协定，对方用了特制客户端，可能不会生效） |
| `DISABLE_CAPTCHA` | `FALSE` | `TRUE` 关掉人机验证。**不建议**，这是防御 spam 的第一道门槛 |
| `MESSAGE_INTERVAL` | `5` | 连续刷屏时的最小间隔（秒），`0` 不限制。前 8 条不排队，第 9 条起才按这个间隔放行 |

> `.env` 已在 `.gitignore` 和 `.dockerignore` 里，不会被打进镜像或提交。

---

## 4. 启动

**必须在项目根目录执行**（`interactive-bot/` 和 `.env` 的上一级）：

```bash
python -m interactive-bot
```

看到类似这样的日志就成功了：

```
2026-11-24 20:00:31,570 interactive-bot INFO ====================================================================
2026-11-24 20:00:31,571 interactive-bot INFO 启动 interactive-bot | pid=13040 | Python 3.12.3 | /bot/私聊双向bot
2026-11-24 20:00:31,571 interactive-bot INFO 配置：验证码=开启 | 间隔=5s | 后台群=-100123465789 | 管理员 1 人
2026-11-24 20:00:31,573 interactive-bot INFO 验证码字体 4 个：Inter-Bold.otf、JetBrainsMono-Bold.ttf、ComicNeue-Bold.ttf、ZillaSlab-Bold.ttf
```

- 那条 `====` 横幅是每次启动的分隔线，带 **pid**。日志是追加写的，
  旧的一次崩溃记录会留在文件里，看到横幅就知道下面这段属于哪一次运行。
- `验证码字体` 那一行**必须是 4 个**。是 0 或 1 个说明部署包里少了 `fonts/`。

进程常驻，`Ctrl+C` 停止。

### 4.1 首次启动会做什么

- 自动创建 `assets/db.sqlite3`、`assets/captcha.db`、`assets/spam.db`
- 自动建表、建索引（WAL 模式，支持并发读写）
- 开始接收私聊消息

不会自动创建后台群话题 —— 那是第一个用户发消息时才触发的。

### 4.2 立刻自检（1 分钟）

**第一步：用管理员账号私聊 bot，发 `/start`。**

这一步会拿 `ADMIN_GROUP_ID` 去群里查一次，是最快的配置验证：

- 回复「目前配置正确，可以在群组 XXX 中使用」→ 群 ID、bot 在群、管理员权限都对了
- 回复「⚠️ 后台管理群组设置错误」→ 群 ID 填错，或 bot 没进群 / 没给管理员权限

**第二步：走一遍完整用户流程**（建议用另一个账号，或让朋友帮忙）

1. 发 `/start` → 收到欢迎语
2. 发任意一句话（比如"在吗"）→ 收到一张 6 位字符的图片
3. 输入图片上的字符（大小写都行，空格自动去掉）→ 回复"验证通过 ✅ 你刚才发的 1 条消息已经一并转发了"
4. 此时**不用再发一遍**，后台群里应已出现新话题「昵称 | user_id」，里面就是第 2 步那句"在吗"
5. 在话题里直接回复一条 → 用户那边收到

> 为了方便只有一个号的人测试，管理员没有设计验证码豁免，你用自己的号测也会走 1-5 步，只是会在后台多出一个
> 你自己的话题，测完在群里删掉那个话题即可。

五步都过，说明部署完成。任何一步卡住，对照第 7 节。

---

## 5. 日常管理

以下命令**只能在后台群里用**，且只有 `ADMIN_USER_IDS` 里的人有效。

| 命令 | 用法 | 作用 |
|---|---|---|
| `/clear` | 在某个用户的话题里发 | 清空这个用户与后台之间的所有消息 |
| `/broadcast` | **回复**一条消息再发 | 把被回复的那条广播给所有已通过验证的用户 |
| `/release_user <user_id>` | 直接发 | 解除该用户的验证封禁/冷却，并放行其被拦截的消息 |
| `/help` | 私聊或群里直接发 | 管理员看到的是这份手册，普通用户看到的是用户版 |

管理命令和用户命令的文案是**两套**：普通用户发 `/help` 只会看到
"怎么发消息、怎么停止接收"，看不到 `/clear`、`/broadcast` 这些，也不会知道
后台有话题这回事。

用户侧只有四个命令（会出现在输入框的菜单里，不用记）：

| 命令 | 作用 |
|---|---|
| `/start` | 开始；也可以把 `/stop` 停掉的状态恢复回来 |
| `/help` | 使用说明 |
| `/privacy` | 隐私协议，telegram协议里要求的，需要说清楚bot存了什么、存多久 |
| `/stop` | 停止接收：之后不再转发他的消息，也不再主动给他发消息 |

`/stop` 之后，管理员在话题里回复该用户会收到一条"该用户已 /stop，这条没有送达"的回执，
不会静默失败。

**每天两个定时任务**（时区按 `BOT_TIMEZONE`）：

- **09:00** 垃圾信息拦截日报：昨天拦了谁、拦了什么，方便你判断哪些可能是误封。
- **04:00** 清理过期数据：垃圾指纹（30/180 天）、已发出的验证码答案（24 小时）

拦截日报里的「⚠️ 建议复核」那部分要关注。被判为「老用户被拦」或「没有高置信信号支撑」的消息会列出来。

### 5.1 隐私协议（`/privacy` ）

| 存了什么 | 存在哪 | 留多久 | 为什么 |
|---|---|---|---|
| 消息对照表（你的消息号 ↔ 后台消息号、你的 ID） | `assets/db.sqlite3` | 直到 `/clear` 或删话题 | 让你回复我时能对上号 |
| 昵称 / 用户名 / is_premium | 同上 | 同上 | 建会话时显示 |
| 消息指纹（64 位哈希，**不含原文**） | `assets/spam.db` | 正常 30 天，被拦 180 天 | 跨账号识别同一段广告 |
| 被判定为广告的消息原文、昵称、理由 | 同上 | 180 天 | 复核与误封申诉 |
| 验证失败次数、通过时间 | `assets/captcha.db` | 长期 | 冷却阶梯与配额判断 |
| 已发出的验证码答案 | 同上 | **24 小时** | 识别"同一答案发给多个号"的批量破解 |
| 相册中转暂存（file_id + 说明） | `assets/db.sqlite3` | 转发成功即删 | 凑齐一组再一次性发出 |
| 是否发过 `/stop` | `assets/db.sqlite3` 的 `user_pref` | 直到 `/start` | 别再打扰他 |

图片、视频、文件本身**不落盘**，只在 Telegram 服务器之间转发。

---

## 6. 长期运行

### 6.1 systemd（推荐的 Linux 方式）

`/etc/systemd/system/tgbot.service`：

```ini
[Unit]
Description=Telegram interactive bot
After=network-online.target

[Service]
Type=simple
User=tgbot
WorkingDirectory=/opt/Telegram-interactive-bot
ExecStart=/opt/Telegram-interactive-bot/.venv/bin/python -m interactive-bot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tgbot
sudo journalctl -u tgbot -f
```

> 路径已锚定到项目目录，`WorkingDirectory` 漏写也不会写错库。但仍然建议写上，方便相对路径的运维脚本。

### 6.2 Docker

```bash
docker build -t tgbot .
docker run -d --name tgbot --restart unless-stopped \
  --env-file .env \
  -v "$PWD/assets:/app/assets" \
  tgbot
```

> Windows PowerShell 里 `$PWD` 不能直接拼进字符串，需要把挂载参数换成
> `-v "${PWD}/assets:/app/assets"`。

`assets/` 必须挂出来，否则容器重建时数据库跟着没了。
字体在镜像里的 `/app/fonts`，不在挂载路径下。

### 6.3 备份

需要备份的只有两个东西：

```
.env          配置（含 token，单独保管）
assets/       全部运行时数据（会话映射、验证状态、垃圾指纹库）
```

热备份 SQLite 不要直接 `cp`（可能拿到写了一半的页），先关闭bot复制或使用：

```bash
sqlite3 assets/db.sqlite3 ".backup 'backup-$(date +%F).db'"
```

### 6.4 升级

```bash
# 关闭Bot
systemctl stop tgbot          # 或 docker stop tgbot

# 备份 assets/，替换代码目录（保留 .env 和 assets/）
# 重新装依赖（requirements.txt 变了才需要）
pip install -r requirements.txt

systemctl start tgbot
```

---

## 7. 故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| 启动即退出，提示 `BOT_TOKEN 未填写` | `.env` 不在项目根，或名字写成了 `.env_example` | 确认文件名是 `.env`，与 `interactive-bot/` 同级 |
| `ADMIN_GROUP_ID 应该是数字` | 填了引号、空格或文字 | 只写数字，带负号：`-1002234110823` |
| `ADMIN_USER_IDS 未填写` | 空值或用了中文逗号 | 英文逗号分隔，`，` 会被自动转换，但别混用空格 |
| 私聊 bot 完全没反应 | token 错；或有两个进程抢同一个 token | 看日志有无 `Conflict: terminated by other getUpdates`，有就 kill 掉多余进程 |
| 启动报 `'Application' object has no attribute '_Application__stop_running_marker'` | PTB 版本太老（21.3）配了 Python 3.13 | `pip install -U "python-telegram-bot[job-queue]==21.11.1"`，这就是 requirements 锁版本的原因 |
| 启动报 `'NoneType' object has no attribute 'run_daily'`，并提示 `No JobQueue set up` | 装 PTB 时漏了 `job-queue` extra，APScheduler 没进来 | `pip install -U "python-telegram-bot[job-queue]==21.11.1"`，注意方括号要带引号 |
| 后台群里回复发不到用户 | **privacy mode 没关**，或 bot 不是群管理员 | 回到 1.2、1.3 重做 |
| `BadRequest: chat not found` | `ADMIN_GROUP_ID` 错，或 bot 已被踢出群 | 重新按 1.4 取 ID，确认 bot 在群里 |
| `not enough rights to create a forum topic` | bot 没有「管理话题」权限，或群没开话题 | 群设置里给权限 + 开启 Topics |
| `the chat is not a forum` | 群没开话题，或填的是升级前的旧群 ID | 开启 Topics 后重新取 ID（会变成 `-100` 开头） |
| `ModuleNotFoundError: No module named 'PIL'`，或启动直接打出一段「✗ 缺少 Pillow」 | 当前解释器没装 Pillow（装了另一个 python） | 用报错里打印的那个解释器路径执行 `<那个路径> -m pip install -r requirements.txt`。装完 `python -c "import PIL"` 必须无报错 |
| 验证码图片发不出来 | `fonts/` 目录被删/挪走 | 启动日志里应显示「验证码字体 4 个」，显示 0 就是字体丢了，把 `fonts/` 拷回去 |
| 日志里 `u2a send failed uid=...: Message thread not found` | 后台把该用户的话题删了，库里还留着旧 thread id | **现在会自动重建话题并把这条补发出去**，不需要人工干预。只有 `DELETE_TOPIC_AS_FOREVER_BAN=TRUE` 时才保留旧语义（删话题=封禁） |
| 反复出现 `not enough rights` / 话题建不出来 | bot 缺「管理话题」权限 | 见 1.3 |
| 日志偶发 `Timed out`、`httpx.ReadError`、`NetworkError` | 跨境线路抖动，PTB 与代码都会自动重试 | 不用管。如果同一条消息重试 3 次仍失败，用户/后台会收到"发送超时，建议重发"，此时才需要看网络 |
| 用户输对了答案还是过不去 | 输入超时（默认 180 秒） | 重新发消息取新图 |
| 用户发消息后被静默忽略 | 被垃圾过滤器拦了 | 看第二天 09:00 日报的「已拦截」；急的话 `/release_user <id>` |
| 修改话题标题后广告又出现了 | 标题经过清洗，命中强关键词会显示为「用户」 | 若仍出现，检查是否跑的是旧代码 |
| `database is locked` | 多实例同时写同一个库 | 确认只有一个进程；WAL 已开启，正常不会出现 |
| 管理员发 `/help` 看到的是用户版，`/clear`、`/release_user` 也没反应 | 运行时那份 `ADMIN_USER_IDS` 里没有你的 ID | 看启动日志那行 `管理员 N 人 123456789`，和你的数字 ID 对一下。**如果 systemd 的 `Environment=` 或 `docker run -e` 里也设了 `ADMIN_USER_IDS`，它会盖掉 `.env`** —— `load_dotenv()` 默认不覆盖已存在的环境变量。发一次 `/help` 会打一行 `help uid=... admin=False`，可直接确认 |

日志在项目根的 `log.txt`，同时输出到 stdout。

---

## 8. 几个必须知道的行为

- **验证流程**：发消息 → 收图 → 输答案 → 刚才那句自动补发。

用户验证前发的消息会先临时存在内存里（最多 5 条、15 分钟内），验证通过后按原顺序
自动转发到后台，**用户不需要重发**。输答案的那条消息会被屏蔽掉，不会转发
（否则你在后台会看到一串 `4H29PS` 这样的验证码）。

- **验证前说的内容不会被当成答错**。只有当一条消息「长度等于验证码长度、且每个
  字符都在字符集里」才会被当做验证码。

- **未通过验证的用户，消息不会进后台**，只会收到一句"请先完成验证"的提示
  （10 秒内最多回一次，避免有人无聊刷屏）。

验证通过之后有两道限流，必须同时满足：

| 限制 | 默认值 | 超了会怎样 |
|---|---|---|
| `MESSAGE_INTERVAL` | 5 秒（**前 8 条不排队**） | **排队等一会，不丢消息**（每用户一把锁，只挡住他自己） |
| 消息配额 | 新号首小时 10 条；之后每 10 分钟 30 条 | 回复一次"发送过于频繁，请 10 分钟后再试"，之后 5 分钟内的超限消息静默丢弃 |

- `MESSAGE_INTERVAL` 不是固定间隔：前 8 条消息是立刻放行的，之后每 5 秒补 1 个。所以"选 5 张图一张一张发"（相册被拆成单条）这种完全正常的操作触发5秒间隔；只有连发的第 9 条起才会被开始要求 5 秒间隔一条。

- **一个相册只算一条配额**。一次"发送 10 张图"算一条消息。

- **同一段广告被多个账号发**时会被自动进入广告指纹库，之后同样的内容一律秒拦，不依赖关键词。

- **删除话题 ≠ 封禁**。默认 `DELETE_TOPIC_AS_FOREVER_BAN=FALSE`，删了话题只是清掉这个会话，
  用户下一条消息到达时会**自动重建话题并把这条消息补发进去**（日志里会有一行
  `话题已不存在，自动重建`），用户无感。相册走的是定时任务，同样会重建后整组补发。
  想彻底封禁才改成 `TRUE` —— 那样删话题后用户会收到"对话已被关闭"，且不会重建。

- **网络抖动不会丢消息**。`TimedOut` / `NetworkError` 会自动退避重试 3 次，日志里是
  WARNING 而不是 ERROR，也不会推"机器人出现异常"告警。只有连重试都失败时，
  用户/后台才会收到"发送超时，建议重发"。

- **日志分级**：`TimedOut`、`httpx.ReadError` 这类可自愈的网络问题是 WARNING；
  真正的代码错误才记 ERROR，并附完整堆栈，同时每 5 分钟最多推一条告警到后台群。
