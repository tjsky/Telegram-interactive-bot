# Telegram Two-Way Private Chat Bot

A private chat relay: a visitor messages the bot, the message lands in a topic of its own in
the admin group, and an admin replying in that topic sends a message back to the visitor.
Ships with human verification, spam filtering and rate limiting.

Modified from [MiHaKun/Telegram-interactive-bot](https://github.com/MiHaKun/Telegram-interactive-bot)
to stop the constant ads and userbot spam.

## Features

- Relay: text, photo, video, document, voice and albums forwarded as-is, with reply support
- One topic per visitor, titled with a sanitised display name; deleting the topic closes the conversation
- Human verification: 6-character image captcha, answer never stored; the same answer sent to
  several accounts is treated as mass solving
- Spam filtering: normalise → fingerprint → cross-account duplicates → keywords; hits are dropped
  silently and summarised once a day at 09:00
- Rate limiting: token bucket plus quota; an album counts as one message
- Commands: `/help` `/privacy` `/stop` `/start`

## Requirements

- Python 3.10 or later (3.10–3.13)
- A Telegram bot, token from @BotFather
- A group with Topics enabled as the admin backend; add the bot as an admin with permission to
  manage topics

## Install

Download `telegram-interactive-bot-<version>.zip` from
[Releases](https://github.com/tjsky/Telegram-interactive-bot/releases), unzip, then:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env_example .env
vi .env                       # at minimum BOT_TOKEN, APP_NAME, ADMIN_GROUP_ID, ADMIN_USER_IDS
python -m interactive-bot
```

`启动 <APP_NAME> | pid=...` in the log means it started. `Ctrl+C` to stop.

## Required configuration

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Token from @BotFather |
| `APP_NAME` | Used for the log and pickle file names, pick anything |
| `ADMIN_GROUP_ID` | Admin group ID, starts with `-100` |
| `ADMIN_USER_IDS` | Admin numeric IDs, comma separated; only these accounts can use admin commands |
| `BOT_TIMEZONE` | Timezone for scheduled jobs, default `Asia/Shanghai` |

Group and user IDs are available from @GetTheirIDBot.
Every other switch is commented in `.env_example`; per-item details are in [`DEPLOY.md`](DEPLOY.md).

## Commands

Visitor:

| Command | Action |
|---|---|
| `/start` | Start, also resumes after `/stop` |
| `/help` | Usage |
| `/privacy` | What is stored and for how long |
| `/stop` | Stop receiving; nothing is forwarded until `/start` |

Admin, used in the admin group:

| Command | Action |
|---|---|
| `/clear` | Send inside a topic: closes the conversation and deletes the topic |
| `/broadcast` | Reply to a message and send: broadcasts it to all users |
| `/release_user <ID>` | Lifts the verification cooldown and clears filtering records |

Deleting a topic is the same as closing the conversation with that user; with
`DELETE_TOPIC_AS_FOREVER_BAN=TRUE` it also permanently bans them.

## Data

| Path | Contents |
|---|---|
| `assets/db.sqlite3` | Users, topic mapping, message mapping |
| `assets/spam.db` | Spam fingerprints and block records |
| `assets/captcha.db` | Verification state |
| `assets/<APP_NAME>.pickle` | Conversation state |
| `log.txt` | Log, rotated at 10MB × 3 |

Backing up `assets/` is enough.

## Running long-term

systemd and Docker instructions are in [`DEPLOY.md`](DEPLOY.md).

## License

Apache License 2.0, see [LICENSE](LICENSE).
Modified from [MiHaKun/Telegram-interactive-bot](https://github.com/MiHaKun/Telegram-interactive-bot).
