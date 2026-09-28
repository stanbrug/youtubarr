#!/bin/sh
# End-to-end environment: mock YouTube API, youtubarr, and a throwaway Plex
# claimed to your Plex account (PLEX_TOKEN, never printed), with an rclone mount of youtubarr.
set -e
cd "$(dirname "$0")/../.."
E2E=/root/yte2e
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"

docker network inspect yte2e >/dev/null 2>&1 || docker network create yte2e >/dev/null
docker build -q -t youtubarr:e2e . >/dev/null
docker rm -f yt-mock youtubarr-e2e plex-e2e >/dev/null 2>&1 || true
rm -rf $E2E && mkdir -p $E2E/data $E2E/plex

docker run -d --name yt-mock --network yte2e -v "$PWD/tests/e2e:/t:ro" python:3.12-slim python -u /t/mock_youtube.py >/dev/null
docker run -d --name youtubarr-e2e --network yte2e -v $E2E/data:/data -v "$PWD/tests:/tests:ro" \
  -e YOUTUBARR_YOUTUBE_API_BASE=http://yt-mock:9900/youtube/v3 -e YOUTUBARR_AUTO_UPDATE=0 \
  -e YOUTUBARR_YOUTUBE_API_KEY=mock-key -e YOUTUBARR_PLEX_URL=http://plex-e2e:32400 \
  -e YOUTUBARR_PLEX_PATH_PREFIX=/youtube -e YOUTUBARR_PLEX_RATE_PER_SECOND=5 youtubarr:e2e >/dev/null

CLAIM=$(curl -fsS -H "X-Plex-Token: $TOKEN" -H "Accept: application/json" -H "X-Plex-Client-Identifier: youtubarr-e2e" \
  https://plex.tv/api/claim/token.json | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
docker run -d --name plex-e2e --hostname youtubarr-e2e-test --network yte2e --memory 2g \
  -e TZ=Europe/Amsterdam -e PLEX_CLAIM="$CLAIM" \
  --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
  -v $E2E/plex:/config plexinc/pms-docker:latest >/dev/null
for i in $(seq 1 60); do
  docker exec plex-e2e sh -c 'grep -q PlexOnlineToken "/config/Library/Application Support/Plex Media Server/Preferences.xml"' 2>/dev/null && break
  sleep 3
done
echo "plex claimed"
docker exec plex-e2e sh -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq curl unzip fuse3 >/dev/null
  curl -fsSL https://rclone.org/install.sh | bash >/dev/null 2>&1
  echo user_allow_other >> /etc/fuse.conf
  mkdir -p /opt/fuse /youtube && cp "$(command -v fusermount3)" /opt/fuse/fusermount3
  PATH=/opt/fuse:$PATH rclone mount :http: /youtube --http-url http://youtubarr-e2e:8080/library/ \
    --allow-other --read-only --vfs-cache-mode off --dir-cache-time 5s --poll-interval 0 \
    --log-file /config/rclone.log --log-level INFO --daemon
  grep -q " /youtube fuse" /proc/mounts && echo "mounted /youtube"
' 2>&1 | grep -v debconf
