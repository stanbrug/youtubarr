#!/bin/sh
# Remove the e2e Plex from your plex.tv account, then everything else.
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"
MID=$(docker exec plex-e2e sh -c 'grep -o "ProcessedMachineIdentifier=\"[^\"]*\"" "/config/Library/Application Support/Plex Media Server/Preferences.xml"' 2>/dev/null | cut -d'"' -f2)
if [ -n "$MID" ]; then
  DEVICE=$(curl -fsS -H "X-Plex-Token: $TOKEN" https://plex.tv/devices.xml | python3 -c "
import sys, xml.etree.ElementTree as ET
for d in ET.fromstring(sys.stdin.read()).iter('Device'):
    if d.get('clientIdentifier') == '$MID':
        print(d.get('id'))
")
  [ -n "$DEVICE" ] && echo "plex.tv device removed: HTTP $(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Plex-Token: $TOKEN" "https://plex.tv/devices/$DEVICE.xml")"
fi
docker rm -f yt-mock youtubarr-e2e plex-e2e >/dev/null 2>&1
docker network rm yte2e >/dev/null 2>&1
docker rmi youtubarr:e2e plexinc/pms-docker:latest python:3.12-slim >/dev/null 2>&1
rm -rf /root/yte2e /root/youtubarr-src
docker ps --format '{{.Names}}'
df -h / | tail -1
