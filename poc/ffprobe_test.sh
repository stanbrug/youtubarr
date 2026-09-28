#!/bin/sh
# Stub + playback check without Plex: ffprobe/ffmpeg read the virtual files
# over HTTP with range requests, the way Plex does through the rclone mount.
set -e
APP=http://127.0.0.1:8080
for v in aqz-KE-bpKQ eRsGyueVLvQ R6MlUcmOul8; do
  curl -s -X POST $APP/api/poc/add -d "{\"url\": \"https://www.youtube.com/watch?v=$v\"}" \
    | python -c "import json,sys; d=json.load(sys.stdin); v=d.get('video',{}); print('added', d.get('ok'), v.get('id'), v.get('title'), v.get('duration'), v.get('width'), v.get('height'), d.get('error',''))"
done
urls=$(python - <<'EOF'
import json, urllib.request, urllib.parse
def ls(p):
    html = urllib.request.urlopen("http://127.0.0.1:8080/library/" + p).read().decode()
    return [urllib.parse.unquote(c.split('"', 1)[0]) for c in html.split('<a href="')[1:]]
for show in ls(""):
    for season in ls(urllib.parse.quote(show)):
        for f in ls(urllib.parse.quote(show) + urllib.parse.quote(season)):
            print("http://127.0.0.1:8080/library/" + urllib.parse.quote(show + season + f))
EOF
)
echo "$urls" | sed 's/.*library\//  /' | python -c "import sys,urllib.parse;[print(urllib.parse.unquote(l.rstrip())) for l in sys.stdin]"
echo "== ffprobe of the stubs"
for u in $urls; do
  ffprobe -v error -show_entries format=duration,size,bit_rate:stream=codec_name,profile,width,height,sample_rate,channels \
    -of compact=p=0 "$u"
done
echo "== modes after probing (must all be 'stub', cache empty)"
curl -s $APP/api/videos | python -c "import json,sys; [print(' ', v['id'], v['mode']) for v in json.load(sys.stdin)]"
ls /data/cache
