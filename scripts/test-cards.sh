#!/usr/bin/env bash
# Send real Telegram cards to your phone, driven by a fake Seerr.
#
# Your live Seerr is never contacted: approving or denying a card here changes
# state only in the mock. Requires TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID in .env.
#
#   ./scripts/test-cards.sh          start the stack and send one card
#   ./scripts/test-cards.sh 2        send the 4K TV request instead
#   ./scripts/test-cards.sh stop     tear it down

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.test.yml)
REQUEST_ID="${1:-1}"

if [[ "$REQUEST_ID" == "stop" ]]; then
    "${COMPOSE[@]}" down
    exit 0
fi

if [[ ! -f .env ]]; then
    echo "No .env found. Copy .env.example and fill in TELEGRAM_BOT_TOKEN." >&2
    exit 1
fi

for key in TELEGRAM_BOT_TOKEN ADMIN_CHAT_ID; do
    if ! grep -qE "^${key}=." .env; then
        echo "${key} is empty in .env; the cards would have nowhere to go." >&2
        exit 1
    fi
done

# A second poller on the same token makes Telegram return 409 and the two
# instances steal each other's button presses.
if docker ps --format '{{.Names}}' | grep -qx seerr-telegram-bot; then
    echo "The live bot container is running and would fight this one for the" >&2
    echo "same token. Stop it first:  docker compose stop" >&2
    exit 1
fi

# Empty TELEGRAM_API_BASE means the real api.telegram.org; the mock Telegram
# service is simply never started.
echo "Starting bot + fake Seerr (real Telegram)..."
TELEGRAM_API_BASE= "${COMPOSE[@]}" up -d --build bot mock-seerr

printf 'Waiting for the bot to come up'
for _ in $(seq 60); do
    if curl -sf localhost:8421/health >/dev/null 2>&1; then
        echo " ok"
        break
    fi
    printf '.'
    sleep 1
done

if ! docker logs seerr-bot-test 2>&1 | grep -q "Telegram bot connected"; then
    echo
    echo "The bot did not reach Telegram. Recent log:" >&2
    docker logs seerr-bot-test 2>&1 | tail -5 >&2
    exit 1
fi
docker logs seerr-bot-test 2>&1 | grep "Telegram bot connected" | tail -1

echo "Sending request $REQUEST_ID as a pending-approval webhook..."
curl -sf -XPOST "localhost:5055/trigger/pending?id=${REQUEST_ID}" >/dev/null
echo "Sent. Check Telegram for the card."
echo
echo "  another card:   ./scripts/test-cards.sh 2"
echo "  webhook test:   curl -XPOST localhost:5055/trigger/test"
echo "  what the mock did:  docker logs -f seerr-bot-test-mock-seerr"
echo "  tear down:      ./scripts/test-cards.sh stop"
