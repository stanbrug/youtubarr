#!/bin/sh
# Run a PoC script inside youtubarr-poc with PLEX_TOKEN (never printed):  sh run.sh plex_test.py setup
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"
SCRIPT="$1"; shift
docker exec -e PLEX_TOKEN="$TOKEN" youtubarr-poc python -u "/poc/$SCRIPT" "$@" 2>&1 | sed "s/$TOKEN/***/g"
