#!/bin/sh
# Remove the PoC test server from your plex.tv account, then delete
# every PoC container, network, image and file on this host.
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"
MID=$(docker exec plex-poc sh -c 'grep -o "ProcessedMachineIdentifier=\"[^\"]*\"" "/config/Library/Application Support/Plex Media Server/Preferences.xml"' | cut -d'"' -f2)
if [ -n "$MID" ]; then
  DEVICE=$(curl -fsS -H "X-Plex-Token: $TOKEN" https://plex.tv/devices.xml \
    | python3 -c "
import sys, xml.etree.ElementTree as ET
for d in ET.fromstring(sys.stdin.read()).iter('Device'):
    if d.get('clientIdentifier') == '$MID':
        print(d.get('id'))
")
  if [ -n "$DEVICE" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "X-Plex-Token: $TOKEN" "https://plex.tv/devices/$DEVICE.xml")
    echo "test server removed from plex.tv account: HTTP $code"
  else
    echo "test server not found on plex.tv account"
  fi
fi
docker rm -f youtubarr-poc plex-poc >/dev/null 2>&1
docker network rm ytpoc >/dev/null 2>&1
docker rmi youtubarr:poc plexinc/pms-docker:latest python:3.12-slim >/dev/null 2>&1
docker image prune -f >/dev/null 2>&1
rm -rf /root/ytpoc
docker ps --format '{{.Names}}'
df -h / | tail -1
