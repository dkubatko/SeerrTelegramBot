# Seerr → Telegram approval bot

A small shim that replaces Seerr's "request pending approval" notification with
one that has **Approve** and **Deny** buttons. Pressing a button calls the Seerr
API directly, so you never have to open the web UI to clear a request.

Works with Overseerr, Jellyseerr, and Seerr — they share the same API.

```
  Seerr  ──webhook (MEDIA_PENDING)──▶  bot  ──▶  Telegram   "Approve? Deny?"
    ▲                                                            │
    └────── POST /api/v1/request/{id}/approve ◀── button press ───┘
```

## What it does

- Sends pending requests to one Telegram chat, with poster, title, requester,
  requested seasons, and 4K flag.
- Approves or declines in Seerr when you tap a button, then rewrites the message
  to show the outcome and who decided it.
- Notices when a request was already resolved in the web UI and says so instead
  of silently doing nothing.
- `/start` replies with your Telegram chat ID, so you know what to put in
  `ADMIN_CHAT_ID` (and in Seerr's per-user Telegram settings).
- `/test` verifies the Seerr connection without creating or changing anything.

Only the admin chat can act on buttons. Anyone else gets a rejection toast.

## Setup

### 1. Create the Telegram bot

Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.

### 2. First deploy, without the chat ID

```bash
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `SEERR_URL`, and `SEERR_API_KEY` (Seerr →
Settings → General → API Key). Leave `ADMIN_CHAT_ID` blank, then:

```bash
docker compose up -d --build
```

### 3. Get your chat ID

Send `/start` to your bot. It replies with the chat ID. Put that in
`ADMIN_CHAT_ID` and restart:

```bash
docker compose up -d
```

Until `ADMIN_CHAT_ID` is set the bot answers `/start` but drops incoming
webhooks, and says so in the log.

> To approve from a **group** instead, add the bot to the group, send `/start`
> there, and use that (negative) ID. Disable privacy mode in BotFather first, or
> the bot will not see the command.

### 4. Point Seerr at the bot

In Seerr → **Settings → Notifications → Webhook**:

| Field | Value |
| --- | --- |
| Enable Agent | on |
| Webhook URL | `http://<bot-host>:8420/webhook` |
| Authorization Header | same string as `WEBHOOK_AUTH_TOKEN`, or blank |
| JSON Payload | **leave the default** |
| Notification Types | **Request Pending Approval** only |

The bot parses Seerr's stock payload, so there is no JSON to edit. If you have
customized it, make sure it still contains `notification_type`, `subject`,
`image`, and the `{{request}}` block with `request_id`.

Finally, switch **off** "Request Pending Approval" in Seerr's built-in Telegram
agent, or you will get two messages for every request — theirs without buttons,
and this one with.

## Testing the connection

Both directions can be checked without creating a single request.

**Seerr can reach the bot** — press **Test** on Seerr's webhook settings page.
Seerr sends a `TEST_NOTIFICATION`, and the bot replies in Telegram with
"Webhook test received". A failure toast in Seerr means the URL, the port
mapping, or the `Authorization` header is wrong.

**The bot can reach Seerr** — send `/test` to the bot. It reports the Seerr
version (proving the URL works) and reads the request counts (proving the API
key works). Both are read-only.

The bot also runs both checks at startup and logs a verdict, so
`docker logs seerr-telegram-bot` tells you immediately whether a deploy is good.

## Running from your Mac against the real Seerr

You do not need the Unraid server to run this for real. Both directions work
over the LAN:

- **Bot → Seerr** is an outbound call, so `SEERR_URL=http://<seerr-lan-ip>:5055`
  just works.
- **Seerr → bot** is inbound, so Seerr must reach *your Mac*. Point the webhook
  URL at your Mac's LAN address, e.g. `http://192.168.1.142:8420/webhook`, and
  publish the port with `-p 8420:8420` (the compose files already do).

```bash
cp .env.example .env     # fill in token, SEERR_URL, SEERR_API_KEY
docker compose up -d --build
```

Two gotchas specific to running on a laptop. macOS may prompt to allow incoming
connections the first time — accept it, or Seerr's webhook test will time out.
And your Mac's IP can change on reconnect, so give it a DHCP reservation or
re-check it with `ipconfig getifaddr en0` if webhooks stop arriving.

Telegram itself needs no inbound access in either setup: the bot polls
`api.telegram.org` outbound, so buttons keep working from anywhere.

When you move to Unraid, only two things change: `SEERR_URL` (if Seerr becomes a
container neighbour) and the webhook URL in Seerr's settings.

## Local testing without a real Seerr

`scripts/mock_seerr.py` stands in for Seerr: it serves the endpoints the bot
calls and can fire webhooks at the bot the same way the real notification agent
does. You can run the whole flow against your real Telegram bot and never touch
your live instance.

### As containers

```bash
docker compose -f docker-compose.test.yml up -d --build
```

This starts three containers — the real bot, a fake Seerr on `:5055`, and a fake
Telegram on `:9111` — so the stack runs with no credentials at all and is
visible in Docker Desktop. Drive it entirely with curl:

```bash
curl -XPOST 'localhost:5055/trigger/pending?id=1'   # Seerr sends a request
curl localhost:9111/_sent                           # see the Telegram message
curl -XPOST 'localhost:9111/_press?data=approve:1'  # press Approve
curl -H 'X-Api-Key: test-api-key' localhost:5055/api/v1/request/1  # status: 2
```

To use your **real** Telegram bot with the fake Seerr, put `TELEGRAM_BOT_TOKEN`
and `ADMIN_CHAT_ID` in `.env` along with an empty `TELEGRAM_API_BASE=`, and the
same stack will message your phone instead.

Tear it down with `docker compose -f docker-compose.test.yml down`.

### From source

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Terminal 1 — fake Seerr on :5055
.venv/bin/python scripts/mock_seerr.py

# Terminal 2 — the bot, pointed at the fake
TELEGRAM_BOT_TOKEN=<your token> \
SEERR_URL=http://127.0.0.1:5055 \
SEERR_API_KEY=test-api-key \
ADMIN_CHAT_ID=<your chat id> \
.venv/bin/python -m app.main
```

Then drive it:

```bash
curl -XPOST 'localhost:5055/trigger/test'             # what Seerr's Test button sends
curl -XPOST 'localhost:5055/trigger/pending?id=1'     # a movie request
curl -XPOST 'localhost:5055/trigger/pending?id=2'     # a 4K TV request with seasons
```

A real message with real buttons arrives in Telegram; pressing one is logged by
the mock as `request 1 -> APPROVE`. The triggers are repeatable — each one
resets that request to pending.

To send a payload without running the mock server at all:

```bash
.venv/bin/python scripts/simulate_webhook.py pending
```

### Test suite

```bash
.venv/bin/python -m unittest discover -s tests
```

`tests/test_end_to_end.py` runs the real webhook server, poll loop, and Seerr
client against mock Seerr and mock Telegram, so the wiring is covered end to end
without any credentials.

## Unraid deployment

Build and push the image, or build it on the server:

```bash
docker build -t seerr-telegram-bot:latest .
```

In **Docker → Add Container**:

- **Repository**: `seerr-telegram-bot:latest`
- **Network**: `bridge`
- **Port**: container `8420` → host `8420`
- **Variables**: `TELEGRAM_BOT_TOKEN`, `SEERR_URL`, `SEERR_API_KEY`,
  `ADMIN_CHAT_ID`

No volumes are needed — the bot keeps nothing on disk.

If Seerr runs on the same Unraid box, `SEERR_URL` can be its LAN address
(`http://192.168.1.10:5055`) or, on a shared custom Docker network, its
container name (`http://jellyseerr:5055`). In the second case also set
`SEERR_PUBLIC_URL` to an address your phone can open, since the "Open in Seerr"
link button is followed by your phone rather than by the container.

Outbound access to `api.telegram.org` is required. No inbound internet exposure
is needed: the bot polls Telegram, and only Seerr needs to reach port 8420.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | — | Token from @BotFather |
| `SEERR_URL` | yes | — | Where the container reaches Seerr; a trailing `/api/v1` is stripped |
| `SEERR_API_KEY` | yes | — | Seerr → Settings → General → API Key |
| `ADMIN_CHAT_ID` | no | — | Chat that receives requests and may press buttons |
| `SEERR_PUBLIC_URL` | no | `SEERR_URL` | Address used for "Open in Seerr" link buttons |
| `WEBHOOK_AUTH_TOKEN` | no | — | If set, Seerr must send it as the `Authorization` header |
| `PORT` | no | `8420` | Webhook listener port |
| `WEBHOOK_PATH` | no | `/webhook` | Webhook path; `POST /` also works |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` logs every payload and update |
| `FORWARD_OTHER_NOTIFICATIONS` | no | `false` | Relay non-pending notification types as plain messages |
| `NOTIFY_ON_START` | no | `false` | Send a short message to the admin chat on boot |
| `SEERR_TIMEOUT` | no | `15` | Seconds before a Seerr call gives up |
| `TELEGRAM_API_BASE` | no | `https://api.telegram.org` | For a self-hosted Bot API server or testing |

## Commands

| Command | Who | Effect |
| --- | --- | --- |
| `/start`, `/id` | anyone | Replies with this chat's ID |
| `/test` | admin | Read-only check of the Seerr connection |
| `/status` | admin | Pending / approved / declined counts |
| `/pending` | admin | Lists up to 10 open requests, each with buttons |
| `/help` | anyone | Command list |

`/pending` is the way to catch up on requests that arrived while the bot was
down, since webhooks are not retried by Seerr.

## Troubleshooting

**Nothing arrives in Telegram.** Check `docker logs seerr-telegram-bot` for
`Approvals will be sent to chat …`. If it warns that `ADMIN_CHAT_ID` is not set,
finish step 3. Then press Test on Seerr's webhook page and watch the log.

**Seerr's Test button fails.** The bot returns 401 for a mismatched
`Authorization` header and 503 when `ADMIN_CHAT_ID` is missing. From the Seerr
host, `curl -v http://<bot-host>:8420/health` isolates a networking problem from
a configuration one.

**Buttons say "not authorized".** The chat pressing them is not
`ADMIN_CHAT_ID`. Send `/start` from the chat you want and compare.

**"Already approved in Seerr".** The request was resolved elsewhere. The bot
refuses to re-decide and updates the message to match reality.

**409 Conflict in the logs.** Two copies of the bot are polling the same token.
Stop one; Telegram allows only a single poller per token. This bites when the
test stack and the real stack are both up — `docker compose -f
docker-compose.test.yml down` first.

**"Cannot reach Telegram, retrying".** Network or DNS is not ready yet; the bot
keeps retrying with backoff. It only gives up, and exits, when Telegram actively
rejects the token.

## Notes on behaviour

The bot keeps its message index in memory only. After a restart the buttons on
older messages still work — the request ID travels in the callback data — but
the edited confirmation will show a shorter title. Nothing is persisted, so the
container can be recreated freely.
