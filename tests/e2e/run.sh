#!/bin/sh
# Run e2e steps with PLEX_TOKEN (never printed).
TOKEN="${PLEX_TOKEN:?set PLEX_TOKEN to the X-Plex-Token of your Plex account}"
for step in "$@"; do
  echo "===== $step"
  docker exec -e PLEX_TOKEN="$TOKEN" youtubarr-e2e python -u /tests/e2e/test.py "$step" 2>&1 | sed "s/$TOKEN/***/g"
done
