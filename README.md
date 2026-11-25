# Telegram 双向私聊机器人

访客私聊机器人，消息进后台群组里属于他的话题；管理员在话题里回复，消息回到访客。
带人机验证、垃圾过滤和限流。

基于 [MiHaKun/Telegram-interactive-bot](https://github.com/MiHaKun/Telegram-interactive-bot) 修改，解决总被广告和 UserBot 骚扰的问题。

## 功能

- 私聊中转：文字、图片、视频、文件、语音、相册原样转发，支持引用回复
- 一个访客一个话题，标题用清洗过的昵称；删话题即关闭会话
- 人机验证：6 位图片验证码，答案不落盘；同一答案发给多个账号判为批量破解
- 垃圾过滤：归一化 → 指纹 → 跨账号同文 → 关键词，命中静默丢弃，每天 09:00 出汇总
- 限流：令牌桶 + 配额，相册整组算一条
- 命令：`/help` `/privacy` `/stop` `/start`

## 环境要求

- Python 3.10 及以上（3.10–3.13 均可）
- 一个 Telegram 机器人，从 @BotFather 取 token
- 一个开启「话题」的群组作后台，机器人设为管理员，权限含「管理话题」

## 安装

从 [Releases](https://github.com/tjsky/Telegram-interactive-bot/releases) 下载
`telegram-interactive-bot-<版本>.zip`，解压后：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env_example .env
vi .env                       # 至少填 BOT_TOKEN、APP_NAME、ADMIN_GROUP_ID、ADMIN_USER_IDS
python -m interactive-bot
```

日志出现 `启动 <APP_NAME> | pid=...` 即启动成功。停止用 `Ctrl+C`。

## 必要配置

| 变量 | 说明 |
|---|---|
| `BOT_TOKEN` | @BotFather 给的 token |
| `APP_NAME` | 日志和 pickle 的文件名，自取 |
| `ADMIN_GROUP_ID` | 后台群组 ID，`-100` 开头 |
| `ADMIN_USER_IDS` | 管理员数字 ID，逗号分隔，只有这些人能用管理命令 |
| `BOT_TIMEZONE` | 定时任务时区，默认 `Asia/Shanghai` |

群组 ID 与用户 ID 可用 @GetTheirIDBot 获取。
其余开关见 `.env_example` 的注释，逐项说明见 [`DEPLOY.md`](DEPLOY.md)。

## 命令

访客：

| 命令 | 作用 |
|---|---|
| `/start` | 开始，也可从 `/stop` 恢复 |
| `/help` | 使用说明 |
| `/privacy` | 保存了哪些信息、保留多久 |
| `/stop` | 停止接收，`/start` 前不再转发 |

管理员，在后台群组里使用：

| 命令 | 作用 |
|---|---|
| `/clear` | 在某个话题里发送：关闭会话并删除话题 |
| `/broadcast` | 回复一条消息再发送：广播给全部用户 |
| `/release_user <ID>` | 解除验证冷却、清除过滤记录 |

直接删话题等于关闭与对应用户的会话，当开启`DELETE_TOPIC_AS_FOREVER_BAN=TRUE` 时会同时触发永久封禁。

## 数据

| 路径 | 内容 |
|---|---|
| `assets/db.sqlite3` | 用户、话题映射、消息对照表 |
| `assets/spam.db` | 垃圾指纹与拦截记录 |
| `assets/captcha.db` | 验证状态 |
| `assets/<APP_NAME>.pickle` | 会话状态 |
| `log.txt` | 日志，10MB × 3 轮转 |

备份只需拷 `assets/`。

## 长期运行

systemd 与 Docker 两种方式见 [`DEPLOY.md`](DEPLOY.md)。

## 许可

Apache License 2.0，见 [LICENSE](LICENSE)。
本项目基于 [MiHaKun/Telegram-interactive-bot](https://github.com/MiHaKun/Telegram-interactive-bot) 修改。
