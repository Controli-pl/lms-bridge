#!/usr/bin/env python3
"""
Klej LMS (CLI/Telnet, port 9090) -> Home Assistant media_player.

Nie dotyka audio w ogóle. Subskrybuje zdarzenia dla jednego, konkretnego
playera (po MAC) na porcie CLI LMS, i na każde play/pause/stop/nowy-utwór
wywołuje odpowiednią usługę media_player w Home Assistant. ESP32 sam ciąga
audio z LMS przez zwykłe HTTP (URL podany w --stream-url), niezależnie od
tego procesu.

Format komend/notyfikacji CLI LMS wg oficjalnej dokumentacji:
https://lyrion.org/reference/cli/general/
https://lyrion.org/reference/cli/notifications/
Przykład z dokumentacji: "subscribe mixer,pause<LF>" -> serwer echo'uje to
samo, a potem asynchronicznie wysyła linie typu:
"04:20:00:12:23:45 mixer volume 25<LF>"
"04:20:00:12:23:45 pause<LF>"

*** DO ZWERYFIKOWANIA NA ŻYWO ***
Zanim to odpalisz w Dockerze, warto sprawdzić realny format ręcznie:
    telnet <LMS_HOST> 9090
    subscribe play,pause,stop,playlist
i zagrać/zapauzować coś w LMS na tym playerze - zobaczysz dokładnie jakie
linie faktycznie przychodzą, zanim zaufamy parsowaniu poniżej.
"""
import argparse
import asyncio
import logging
import sys
from urllib.parse import unquote

import aiohttp

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("lms-control-bridge")

RECONNECT_DELAY_S = 5


async def call_ha_service(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    domain: str,
    service: str,
    entity_id: str,
    extra: dict | None = None,
) -> None:
    url = f"{ha_url.rstrip('/')}/api/services/{domain}/{service}"
    payload = {"entity_id": entity_id}
    if extra:
        payload.update(extra)
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 300:
                body = await resp.text()
                log.warning("HA %s.%s -> HTTP %s: %s", domain, service, resp.status, body)
            else:
                log.info("HA %s.%s OK (%s)", domain, service, entity_id)
    except Exception:
        log.exception("Błąd wywołania Home Assistant %s.%s", domain, service)


async def handle_notification(
    line: str,
    player_mac: str,
    stream_url: str,
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    media_player_entity: str,
) -> None:
    fields = [unquote(tok) for tok in line.strip().split(" ")]
    if not fields:
        return

    mac = fields[0]
    if mac.lower() != player_mac.lower():
        return  # notyfikacja dla innego playera - ignorujemy

    if len(fields) < 2:
        return
    event = fields[1]

    if event == "play":
        log.info("LMS: play -> wysyłam świeże play_media do %s (wymusza reconnect/flush bufora)", media_player_entity)
        await call_ha_service(
            session, ha_url, ha_token, "media_player", "play_media", media_player_entity,
            extra={"media_content_id": stream_url, "media_content_type": "music"},
        )
    elif event == "pause":
        paused = fields[2] if len(fields) > 2 else "1"
        if paused == "0":
            log.info("LMS: pause 0 (wznowienie) -> media_play")
            await call_ha_service(session, ha_url, ha_token, "media_player", "media_play", media_player_entity)
        else:
            log.info("LMS: pause -> media_pause")
            await call_ha_service(session, ha_url, ha_token, "media_player", "media_pause", media_player_entity)
    elif event == "stop":
        log.info("LMS: stop -> media_stop")
        await call_ha_service(session, ha_url, ha_token, "media_player", "media_stop", media_player_entity)
    elif event == "playlist" and len(fields) > 2 and fields[2] == "newsong":
        log.info("LMS: playlist newsong -> wysyłam świeże play_media (nowy utwór, wymuszamy reconnect)")
        await call_ha_service(
            session, ha_url, ha_token, "media_player", "play_media", media_player_entity,
            extra={"media_content_id": stream_url, "media_content_type": "music"},
        )
    elif event == "mixer" and len(fields) > 3 and fields[2] == "volume":
        raw_volume = fields[3]
        try:
            # LMS zgłasza 0-100; może być też względne (np. "+5"/"-5") przy
            # niektórych operacjach - to obsługujemy tu tylko wartość
            # absolutną. Względne zmiany na razie ignorujemy (do ew.
            # dopracowania, jeśli się okażą potrzebne w praktyce).
            level = int(raw_volume)
            volume_level = max(0.0, min(1.0, level / 100.0))
        except ValueError:
            log.debug("Nie potrafię sparsować poziomu głośności: %s", raw_volume)
            return
        log.info("LMS: mixer volume %s -> volume_set %.2f", raw_volume, volume_level)
        await call_ha_service(
            session, ha_url, ha_token, "media_player", "volume_set", media_player_entity,
            extra={"volume_level": volume_level},
        )
    else:
        log.debug("Nieobsłużone zdarzenie: %s", line.strip())


async def run(
    lms_host: str,
    lms_cli_port: int,
    player_mac: str,
    stream_url: str,
    ha_url: str,
    ha_token: str,
    media_player_entity: str,
) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info("Łączę z LMS CLI %s:%s", lms_host, lms_cli_port)
                reader, writer = await asyncio.open_connection(lms_host, lms_cli_port)

                subscribe_cmd = "subscribe play,pause,stop,playlist,mixer\n"
                writer.write(subscribe_cmd.encode("utf-8"))
                await writer.drain()
                log.info("Wysłano: %s", subscribe_cmd.strip())

                while True:
                    raw_line = await reader.readline()
                    if not raw_line:
                        raise ConnectionError("LMS CLI zamknęło połączenie")
                    line = raw_line.decode("utf-8", errors="replace")
                    log.debug("CLI: %s", line.strip())
                    await handle_notification(
                        line, player_mac, stream_url, session, ha_url, ha_token, media_player_entity
                    )
            except Exception:
                log.exception("Połączenie z LMS CLI padło, ponawiam za %ss", RECONNECT_DELAY_S)
                await asyncio.sleep(RECONNECT_DELAY_S)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lms-host", required=True)
    parser.add_argument("--lms-cli-port", type=int, default=9090)
    parser.add_argument("--player-mac", required=True)
    parser.add_argument("--stream-url", required=True)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--media-player-entity", required=True)
    args = parser.parse_args()

    asyncio.run(run(
        args.lms_host, args.lms_cli_port, args.player_mac, args.stream_url,
        args.ha_url, args.ha_token, args.media_player_entity,
    ))
