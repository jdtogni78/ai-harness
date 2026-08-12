# Telegram inbound bridge (Phase-1 spike)

Status: spike, 2026-08-11 (#163). Lands the boss's Telegram messages into the
existing durable answers feed + phone push, and can reply. Phase-B / the
propose-and-confirm scheduler (#164) builds on this bridge's feed/reply seam.

## Why poll, not webhook

The home host is behind NAT (no public URL / TLS cert to receive a webhook), so
the bridge **long-polls** the Bot API `getUpdates` — the same shape as the
usage-limit monitor (`remote_control/usage_limit/monitor.py`): a supervised
loop, JSON state + pid-lock, SIGTERM-clean shutdown, env→config.

Code: `remote_control/telegram/bridge.py` · CLI: `python3 -m remote_control
telegram-bridge` · launchd: `com.<user>.claude-telegram-bridge.plist`.

**Install-on-demand for the spike (#163):** the plists ship and the installer
*supports* them, but the bridge is intentionally NOT in the installer's default
`AGENTS` list yet — so a normal `install` does not auto-load it fleet-wide.
Bootstrap it manually once a token is in place (e.g. `install
com.<user>.claude-telegram-bridge.plist`, or `launchctl bootstrap`). It gets
enrolled in `AGENTS` as a follow-up once validated with a real token.

## Data flow

```
Telegram getUpdates (long poll)
  → normalize_message()      # stable inbound shape; text sanitized
  → is_allowed()             # chat_id vs TELEGRAM_ALLOWED_CHAT_IDS (empty=all)
  → feed_post()              # argv → answers.sh post  (no shell)
        └─ answers.sh does all 3 durable effects:
           iCloud ANSWERS.md  +  pinned inbox session  +  phone PushNotification
```

An inbound boss message is filed as a feed entry with the message as the **Q**
and a pending **A** (`(inbound — awaiting reply)`) — the reply/confirm #164 will
fill. The persisted `offset` (state file) advances past every consumed
`update_id`, including dropped/rejected ones, so nothing is re-fetched.

## Going live (boss-provided input)

The bot token is **never** in the repo, a plist, a log, or chat. The boss:

1. Creates the bot via **@BotFather**, copies the token.
2. Drops it in a chmod-600 file:
   ```bash
   mkdir -p ~/.ai-harness/telegram
   printf '%s' '<TOKEN>' > ~/.ai-harness/telegram/bot_token
   chmod 600 ~/.ai-harness/telegram/bot_token
   ```
3. (Recommended) sets `TELEGRAM_ALLOWED_CHAT_IDS` to the boss's chat id(s) in
   the plist so only the boss's messages are accepted.

Until that file exists the bridge **idles** (logs "live poll gated"), so the
LaunchAgent is safe to load before the token is dropped. The bridge reads the
token fresh each cycle and never logs it (the token sits in the Bot API URL
path, so the URL is never logged either).

## Config (env → `TelegramConfig`)

| env | default | meaning |
|-----|---------|---------|
| `TELEGRAM_TOKEN_FILE` | `~/.ai-harness/telegram/bot_token` | chmod-600 token file |
| `TELEGRAM_DRY_RUN` | `1` (on) | land inbound to feed; **send no replies** until `0` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | *(empty = all)* | comma-separated allowed chat ids |
| `TELEGRAM_POLL_TIMEOUT_SECS` | `30` | long-poll hold time |
| `TELEGRAM_HTTP_TIMEOUT_SECS` | `45` | socket timeout margin on top of the poll |
| `TELEGRAM_MAX_MESSAGE_LEN` | `2000` | inbound cap before feed/render/shell |
| `TELEGRAM_FEED_TAG` | `TELEGRAM` | manager tag messages are filed under |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | overridable for tests |

## Security posture

- **Untrusted input:** `sanitize_text()` strips NUL/C0/C1 control chars (keeps
  `\t`/`\n`) and caps length before any feed write, render, or subprocess. The
  feed poster is invoked via **argv, never a shell string**, so there's no
  shell-injection surface.
- **No secrets in logs:** token never logged; API errors log the method name
  only, never the token-bearing URL.
- **Auth is on `chat_id`,** not the spoofable display name.
- **Tests make no live authenticated calls** (cf #114): the Bot API client is
  exercised through a fake `urlopen`/injected `run`, with a mock token or none.

## The seam #164 consumes

- `normalize_message(update, cfg)` → the stable inbound record written to the
  feed (`update_id`, `chat_id`, `sender`, `sender_id`, `text`, `date`).
- `send_message(cfg, token, chat_id, text, log)` → outbound `sendMessage` for
  replies/confirms. Gated by `TELEGRAM_DRY_RUN` at the loop level (#164 flips it
  on). #164 stays strictly **propose-and-confirm (no auto-book)** — this bridge
  only plumbs messages in and replies out; it makes no booking decisions.

Tests: `tests/test_telegram_bridge.py`.
