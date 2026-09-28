#!/bin/sh
# Recreate plex-poc claimed to your Plex account. PLEX_TOKEN is only ever
# passed around inside this script (never printed).
set -e
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"
CLAIM=$(curl -fsS -H "X-Plex-Token: $TOKEN" -H "Accept: application/json" \
  -H "X-Plex-Client-Identifier: youtubarr-poc" https://plex.tv/api/claim/token.json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
case "$CLAIM" in claim-*) echo "claim code obtained";; *) echo "no claim code"; exit 1;; esac

docker rm -f plex-poc >/dev/null 2>&1 || true
rm -rf /root/ytpoc/plex && mkdir -p /root/ytpoc/plex
docker run -d --name plex-poc --hostname youtubarr-poc-test --network ytpoc --memory 2g \
  -e TZ=Europe/Amsterdam -e PLEX_CLAIM="$CLAIM" \
  --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
  -v /root/ytpoc/plex:/config plexinc/pms-docker:latest >/dev/null
for i in $(seq 1 60); do
  docker exec plex-poc sh -c 'grep -q PlexOnlineToken "/config/Library/Application Support/Plex Media Server/Preferences.xml"' 2>/dev/null && break
  sleep 3
done
docker exec plex-poc sh -c 'grep -q PlexOnlineToken "/config/Library/Application Support/Plex Media Server/Preferences.xml"' \
  && echo "test server claimed" || echo "claim did not complete"
