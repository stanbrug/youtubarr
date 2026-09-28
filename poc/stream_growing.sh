#!/bin/sh
# Transcoder reading a cold video from 05:00 straight off the growing file
# (no waiting for the finished download) -- the worst case for long videos.
cd "$(dirname "$0")"
docker exec youtubarr-poc curl -s -X POST http://127.0.0.1:8080/api/settings -d '{"wait_for_complete_secs": "0"}' >/dev/null
sh run.sh plex_test.py stream 2>&1 | tail -8
echo "reads of the file:"
docker exec youtubarr-poc curl -s http://127.0.0.1:8080/api/events | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if e["video"] == "R6MlUcmOul8" and e.get("mode") in ("download", "cached"):
        print("  ", {k: e.get(k) for k in ("mode", "range", "sent", "done")})
' | head -12
docker exec youtubarr-poc curl -s -X POST http://127.0.0.1:8080/api/settings -d '{"wait_for_complete_secs": "20"}' >/dev/null
