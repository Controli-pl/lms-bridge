FROM python:3.12-slim

# squeezelite: TYLKO po to, żeby LMS widział zarejestrowanego playera.
# Jego audio output nas nie interesuje (leci do /dev/null) - prawdziwe audio
# ESP32 ciągnie samo, bezpośrednio z LMS przez HTTP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends squeezelite procps pv \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir aiohttp

COPY entrypoint.sh /entrypoint.sh
COPY bridge.py /bridge.py
RUN chmod a+x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
