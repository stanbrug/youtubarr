#!/bin/sh
# Run a script from tests/e2e inside youtubarr-e2e with PLEX_TOKEN
# (never printed):  sh py.sh test.py play Sintel
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"
SCRIPT="$1"; shift
docker exec -e PLEX_TOKEN="$TOKEN" youtubarr-e2e python -u "/tests/e2e/$SCRIPT" "$@" 2>&1 | sed "s/$TOKEN/***/g"
