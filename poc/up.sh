#!/bin/sh
# Proof of concept environment: youtubarr + a throwaway Plex Media Server with
# an rclone HTTP mount of youtubarr's /library/. Nothing is published on the
# host; the test talks to both over the Docker network.
set -e
cd "$(dirname "$0")/.."
docker network inspect ytpoc >/dev/null 2>&1 || docker network create ytpoc >/dev/null
docker build -q -t youtubarr:poc . >/dev/null
docker rm -f youtubarr-poc plex-poc >/dev/null 2>&1 || true
mkdir -p /root/ytpoc/data /root/ytpoc/plex

docker run -d --name youtubarr-poc --network ytpoc -v /root/ytpoc/data:/data -v "$PWD/poc:/poc:ro" \
  -e YOUTUBARR_AUTO_UPDATE=0 -e LOG_LEVEL=INFO youtubarr:poc >/dev/null

docker run -d --name plex-poc --network ytpoc --memory 2g \
  -e TZ=Europe/Amsterdam -e ALLOWED_NETWORKS=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8 \
  --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined \
  -v /root/ytpoc/plex:/config plexinc/pms-docker:latest >/dev/null

# rclone inside the Plex container, mounting youtubarr's virtual library.
docker exec plex-poc sh -c '
  command -v rclone >/dev/null || {
    apt-get update -qq >/dev/null && apt-get install -y -qq curl unzip fuse3 >/dev/null
    curl -fsSL https://rclone.org/install.sh | bash >/dev/null 2>&1
  }
  grep -q "^user_allow_other" /etc/fuse.conf 2>/dev/null || echo user_allow_other >> /etc/fuse.conf
  # Ubuntu hosts ship an AppArmor profile for /usr/bin/fusermount3 that also
  # attaches inside containers and denies the mount; a copy elsewhere on
  # PATH is not matched by it.
  mkdir -p /opt/fuse /youtube && cp "$(command -v fusermount3)" /opt/fuse/fusermount3
  PATH=/opt/fuse:$PATH rclone mount :http: /youtube --http-url http://youtubarr-poc:8080/library/ \
    --allow-other --read-only --vfs-cache-mode off --dir-cache-time 10s --poll-interval 0 \
    --log-file /config/rclone.log --log-level INFO --daemon
'
docker ps --filter name=youtubarr-poc --filter name=plex-poc --format '{{.Names}} {{.Status}}'
