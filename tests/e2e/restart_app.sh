#!/bin/sh
# (Re)create only the youtubarr-e2e container, keeping Plex and the mock.
cd "$(dirname "$0")/../.."
docker build -q -t youtubarr:e2e . >/dev/null
docker rm -f youtubarr-e2e >/dev/null 2>&1 || true
docker restart yt-mock >/dev/null  # back to the initial catalog
rm -rf /root/yte2e/data && mkdir -p /root/yte2e/data
docker run -d --name youtubarr-e2e --network yte2e -v /root/yte2e/data:/data -v "$PWD/tests:/tests:ro" \
  -e YOUTUBARR_YOUTUBE_API_BASE=http://yt-mock:9900/youtube/v3 -e YOUTUBARR_AUTO_UPDATE=0 \
  -e YOUTUBARR_YOUTUBE_API_KEY=mock-key -e YOUTUBARR_PLEX_URL=http://plex-e2e:32400 \
  -e YOUTUBARR_PLEX_PATH_PREFIX=/youtube -e YOUTUBARR_PLEX_RATE_PER_SECOND=5 youtubarr:e2e >/dev/null
sleep 2
echo "== unit tests inside the image (incl. ffmpeg stub probe)"
docker exec -w /app youtubarr-e2e python -m unittest discover -s /tests 2>&1 | tail -3
