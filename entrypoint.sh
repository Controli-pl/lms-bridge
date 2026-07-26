#!/usr/bin/env bash
set -e

: "${LMS_HOST:?musisz ustawić LMS_HOST}"
: "${LMS_PORT:=3483}"
: "${LMS_CLI_PORT:=9090}"
: "${PLAYER_NAME:=LMS Sendspin Bridge}"
: "${PLAYER_MAC:=02:00:00:00:00:01}"
: "${SAMPLE_RATE:=44100}"
: "${HA_URL:?musisz ustawić HA_URL, np. http://192.168.1.122:8123}"
: "${HA_TOKEN:?musisz ustawić HA_TOKEN (long-lived access token z Home Assistant)}"
: "${ESP_MEDIA_PLAYER_ENTITY:?musisz ustawić ESP_MEDIA_PLAYER_ENTITY, np. media_player.kuchnia}"

cleanup() {
    echo "[entrypoint] Zatrzymuję squeezelite (PID ${SQUEEZELITE_PID:-?})"
    [ -n "${SQUEEZELITE_PID:-}" ] && kill "$SQUEEZELITE_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

echo "[entrypoint] squeezelite (rejestracja w LMS, audio odrzucane) + bridge.py (CLI -> Home Assistant)"

# squeezelite: rejestruje się w LMS jako player (żeby był widoczny i sterowalny
# z UI LMS), ale jego audio nas nie interesuje - stąd -o - > /dev/null.
squeezelite \
    -s "${LMS_HOST}:${LMS_PORT}" \
    -n "${PLAYER_NAME}" \
    -m "${PLAYER_MAC}" \
    -o - \
    -a 16 \
    -r "${SAMPLE_RATE}-${SAMPLE_RATE}" \
    -c pcm,mp3,flac,ogg,alac \
    -d slimproto=info \
    2>/tmp/squeezelite.log \
    | pv -q -L "$(( SAMPLE_RATE * 2 * 2 ))" \
    > /dev/null &
SQUEEZELITE_PID=$!
echo "[entrypoint] squeezelite wystartował (PID ${SQUEEZELITE_PID})"

# Dajemy squeezelite chwilę na zarejestrowanie się w LMS, zanim zaczniemy
# subskrybować zdarzenia dla jego MAC-a.
sleep 2

exec python3 /bridge.py \
    --lms-host "${LMS_HOST}" \
    --lms-cli-port "${LMS_CLI_PORT}" \
    --player-mac "${PLAYER_MAC}" \
    --stream-url "http://${LMS_HOST}:9000/stream.mp3?player=${PLAYER_MAC}" \
    --ha-url "${HA_URL}" \
    --ha-token "${HA_TOKEN}" \
    --media-player-entity "${ESP_MEDIA_PLAYER_ENTITY}"
