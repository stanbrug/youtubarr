#!/bin/sh
# rclone HTTP mount of youtubarr's /library/ inside the plex-poc container.
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
  grep -q " /youtube fuse" /proc/mounts && echo "mounted /youtube"
'
