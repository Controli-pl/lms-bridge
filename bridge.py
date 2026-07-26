#!/usr/bin/env python3
"""
Klej LMS (CLI/Telnet, port 9090) -> Home Assistant media_player.

Przy playlist newsong / play / resume: media_stop + pauza + play_media
— wymusza zerwanie starego połączenia HTTP na ESP.
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
NEWSONG_RECONNECT_GAP_S = 0.5


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


async def force_play_stream(
    session: aiohttp.ClientSession,
    ha_url: str,
    ha_token: str,
    media_player_entity: str,
    stream_url: str,
    reason: str,
) -> None:
    log.info("LMS: %s -> media_stop + play_media (reconnect)", reason)
    await call_ha_service(session, ha_url, ha_token, "media_player", "media_stop", media_player_entity)
    await asyncio.sleep(NEWSONG_RECONNECT_GAP_S)
    await call_ha_service(
        session, ha_url, ha_token, "media_player", "play_media", media_player_entity,
        extra={"media_content_id": stream_url, "media_content_type": "music"},
    )


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
        return

    if len(fields) < 2:
        return
    event = fields[1]

    if event == "play":
        await force_play_stream(
            session, ha_url, ha_token, media_player_entity, stream_url, "play"
        )
    elif event == "pause":
        paused = fields[2] if len(fields) > 2 else "1"
        if paused == "0":
            await force_play_stream(
                session, ha_url, ha_token, media_player_entity, stream_url, "pause 0 (resume)"
            )
        else:
            log.info("LMS: pause -> media_stop (twardsze niż media_pause)")
            await call_ha_service(
                session, ha_url, ha_token, "media_player", "media_stop", media_player_entity
            )
    elif event == "stop":
        log.info("LMS: stop -> media_stop")
        await call_ha_service(
            session, ha_url, ha_token, "media_player", "media_stop", media_player_entity
        )
    elif event == "playlist" and len(fields) > 2 and fields[2] == "newsong":
        await force_play_stream(
            session, ha_url, ha_token, media_player_entity, stream_url, "playlist newsong"
        )
    elif event == "mixer" and len(fields) > 3 and fields[2] == "volume":
        raw_volume = fields[3]
        try:
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
