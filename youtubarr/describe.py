"""Description cleanup for Plex summaries.

The original stays in SQLite; this runs whenever metadata is (re)pushed, so
rule changes apply to everything without YouTube API calls.

Rules (global defaults in Settings, overridable per subscription):
  urls        remove http(s):// and www. links
  sponsor     drop lines containing a sponsor/affiliate keyword
  social      drop the trailing block of social-media lines
  hashtags    drop trailing hashtag lines and hashtags at the very end
  timestamps  drop chapter lines (0:00 / 00:00 / 0:00:00 ...)
  max_length  cut at a sentence/word boundary (~1000 characters)
"""

import re

URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"(^|\s|\()\d{1,2}:\d{2}(:\d{2})?(\s|\)|$|[-–—:])")
HASHTAG_LINE_RE = re.compile(r"^\s*(#[\w\-]+[\s,]*)+$", re.UNICODE)
TRAILING_TAGS_RE = re.compile(r"(\s+#[\w\-]+)+\s*$", re.UNICODE)
SOCIAL_RE = re.compile(
    r"\b(instagram|insta|twitter|x\.com|tiktok|facebook|discord|twitch|snapchat|threads|"
    r"patreon|linkedin|reddit|merch|subscribe|abonneer|follow (me|us)|volg (mij|ons)|"
    r"socials?|business inquiries|zakelijk|contact:?|e-?mail:?)\b",
    re.IGNORECASE,
)
HANDLE_RE = re.compile(r"(^|\s)@[\w.]+", re.UNICODE)


def effective_rules(global_rules, overrides):
    rules = dict(global_rules)
    for key, value in (overrides or {}).items():
        if value is not None and key in rules:
            rules[key] = value
    return rules


TRAILING_URL_RE = re.compile(r"(https?://\S+|www\.\S+)\s*$", re.IGNORECASE)


def _is_link_line(line):
    """A link with at most a short label around it: "► Shop: https://...",
    "Instagram - https://...", "Support us at https://...". Such a line means
    nothing without its link, so it goes entirely. A real sentence that
    merely contains a link keeps its text."""
    if not URL_RE.search(line):
        return False
    words = re.findall(r"\w+", URL_RE.sub(" ", line), re.UNICODE)
    ends_with_link = bool(TRAILING_URL_RE.search(line.rstrip()))
    return len(words) <= 2 or (len(words) <= 5 and (ends_with_link or line.rstrip().endswith(":")))


def _is_social(line):
    stripped = line.strip()
    if not stripped:
        return True  # blank lines inside the block don't end it
    return bool(SOCIAL_RE.search(stripped) or HANDLE_RE.search(stripped) or _is_link_line(stripped)
                or HASHTAG_LINE_RE.match(stripped))


def clean(text, rules):
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if rules.get("timestamps"):
        lines = [l for l in lines if not TIMESTAMP_RE.search(l)]

    if rules.get("sponsor"):
        keywords = [k.lower() for k in rules.get("sponsor_keywords") or [] if k.strip()]
        lines = [l for l in lines if not any(k in l.lower() for k in keywords)]

    if rules.get("social"):
        # Walk up from the end while lines look like a social/link block;
        # checked before URL removal so link-only lines are recognized.
        end = len(lines)
        while end > 0 and _is_social(lines[end - 1]):
            end -= 1
        lines = lines[:end]

    if rules.get("urls"):
        kept = []
        for line in lines:
            if not URL_RE.search(line):
                kept.append(line)
            elif not _is_link_line(line):
                kept.append(URL_RE.sub("", line).rstrip(" :-–—|►▶→>"))
            # link lines with only a label disappear entirely
        lines = kept

    if rules.get("hashtags"):
        while lines and (not lines[-1].strip() or HASHTAG_LINE_RE.match(lines[-1])):
            lines.pop()
        if lines:
            lines[-1] = TRAILING_TAGS_RE.sub("", lines[-1])

    # Collapse runs of blank lines and trim.
    out, blank = [], False
    for line in lines:
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    result = "\n".join(out).strip()

    limit = int(rules.get("max_length") or 0)
    if limit and len(result) > limit:
        cut = result[:limit]
        boundary = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
        if boundary > limit * 0.6:
            cut = cut[:boundary + 1]
        else:
            cut = cut[:cut.rfind(" ")] if " " in cut else cut
            cut = cut.rstrip(",;:- ") + "…"
        result = cut
    return result
