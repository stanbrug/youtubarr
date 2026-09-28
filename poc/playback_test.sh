#!/bin/sh
# Playback path without Plex: pretend Plex analyzed the videos, then read them
# like a player would -- from the start, and with a seek while the download
# is still running.
APP=http://127.0.0.1:8080
python -c "import sqlite3,time; c=sqlite3.connect('/data/youtubarr.db'); c.execute('UPDATE videos SET plex_analyzed_at=?', (time.time(),)); c.commit()"
url() {
  python - "$1" <<'EOF'
import sys, urllib.request, urllib.parse
def ls(p):
    html = urllib.request.urlopen("http://127.0.0.1:8080/library/" + p).read().decode()
    return [urllib.parse.unquote(c.split('"', 1)[0]) for c in html.split('<a href="')[1:]]
for show in ls(""):
    for season in ls(urllib.parse.quote(show)):
        for f in ls(urllib.parse.quote(show) + urllib.parse.quote(season)):
            if sys.argv[1] in f:
                print("http://127.0.0.1:8080/library/" + urllib.parse.quote(show + season + f))
EOF
}
now() { date +%s.%N; }
elapsed() { python -c "print(f'{$(now) - $1:.1f}s')"; }

U=$(url aqz-KE-bpKQ)
echo "== 1. Big Buck Bunny: play from the start (download starts now)"
t=$(now); ffprobe -v error -show_entries format=duration:stream=codec_name,width,height,r_frame_rate -of compact=p=0 "$U"; echo "   probe of the real (growing) file took $(elapsed $t)"
t=$(now); ffmpeg -v error -y -i "$U" -t 10 -c copy /tmp/start.mkv; echo "   first 10s copied in $(elapsed $t)"
echo "== 2. seek to 05:00 while downloading"
t=$(now); ffmpeg -v error -y -ss 300 -i "$U" -t 10 -c copy /tmp/seek.mkv; echo "   10s from 05:00 copied in $(elapsed $t)"
ffprobe -v error -show_entries format=duration:stream=codec_name -of compact=p=0 /tmp/seek.mkv
echo "== 3. seek back to 01:00"
t=$(now); ffmpeg -v error -y -ss 60 -i "$U" -t 5 -c copy /tmp/back.mkv; echo "   5s from 01:00 copied in $(elapsed $t)"
echo "== 4. Tears of Steel: seek straight to 10:00 on a cold video"
U2=$(url R6MlUcmOul8)
t=$(now); ffmpeg -v error -y -ss 600 -i "$U2" -t 5 -c copy /tmp/cold.mkv; echo "   5s from 10:00 copied in $(elapsed $t)"
echo "== waiting for downloads to finish"
for i in $(seq 1 120); do
  modes=$(curl -s $APP/api/videos | python -c "import json,sys; print(' '.join(v['id']+':'+v['mode'] for v in json.load(sys.stdin)))")
  echo "$modes" | grep -q download || break
  sleep 5
done
echo "   $modes"
ls -la /data/cache
echo "== 5. finished file: real size, duration, cues"
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height -of compact=p=0 "$U"
t=$(now); ffmpeg -v error -y -ss 500 -i "$U" -t 5 -c copy /tmp/cached.mkv; echo "   5s from 08:20 on cached file in $(elapsed $t)"
curl -s -I "$U" | grep -i content-length
