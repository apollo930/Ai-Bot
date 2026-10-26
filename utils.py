import html as html_lib, random, requests, json, os, re, sqlite3, time, urllib.parse
from dotenv import load_dotenv
load_dotenv()

def get_cpu_temp() -> str:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        temp = int(f.read().strip()) / 1000
    return f"{temp:.1f}°C"

def get_quote():
	response = requests.get("https://zenquotes.io/api/random")
	json_data = json.loads(response.text)
	quote = "`" + json_data[0]['q'] + "`" + " -" + json_data[0]['a']
	return quote

KEEP_PARAMS = {"v", "t", "list", "index", "start", "time_continue", "img_index", "story_item"}

# Stops at whitespace and at the characters Discord markup wraps links in
# (<> suppress, || spoiler, ` code), so those never end up inside the URL.
URL_RE = re.compile(r'https?://[^\s<>|`"]+', re.IGNORECASE)
CODE_RE = re.compile(r'```.*?```|`[^`\n]*`', re.DOTALL)
# Discord's crawler identifies as Discordbot; fixers key their embed HTML off it.
PROBE_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
EMBED_MEDIA_KEYS = {
    "og:image", "og:image:url", "og:image:secure_url",
    "og:video", "og:video:url", "og:video:secure_url",
    "twitter:image", "twitter:player", "twitter:player:stream",
}
META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
# FxEmbed (fixupx, x.reaper113.xyz) gives Discord photos/videos through a Mastodon-style
# ActivityPub link instead of og:image/og:video, and only adds it when the post was found.
ACTIVITY_RE = re.compile(r"<link\b[^>]*application/activity\+json[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def rule_hosts(rule: dict) -> list[str]:
    """A rule's fixer hosts in the order to try them. Older config.json rules
    hold one string; newer ones a list (from "/urlrule add" with commas)."""
    r = rule["replacement"]
    hosts = r if isinstance(r, list) else r.split(",")
    return [h.strip() for h in hosts if h.strip()]


def matches_domain(host: str, domain: str) -> bool:
    host, domain = host.lower(), domain.lower()
    if host == domain or host.endswith("." + domain):
        return True
    # "www.threads.com" style rules should also catch the bare domain
    return domain.startswith("www.") and host == domain[4:]


def find_links(text: str, rules: list[dict]) -> list[tuple[int, int, str, dict]]:
    """Every link in text that some rule covers, as (start, end, url, rule).
    Skips links the poster kept from embedding (<https://...>) and code spans."""
    code = [m.span() for m in CODE_RE.finditer(text)]
    found = []
    for m in URL_RE.finditer(text):
        start = m.start()
        if any(a <= start < b for a, b in code) or (start > 0 and text[start - 1] == "<"):
            continue
        url = m.group().rstrip(".,;:!?'*")
        if url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
        parsed = urllib.parse.urlparse(url)
        if not parsed.hostname or not parsed.path.strip("/"):
            continue
        for rule in rules:
            if matches_domain(parsed.hostname, rule["domain"]):
                found.append((start, start + len(url), url, rule))
                break
    return found


def spoiler_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each ||spoiler|| in text, as Discord pairs the bars: first with second,
    third with fourth. Bars inside code spans don't count."""
    code = [m.span() for m in CODE_RE.finditer(text)]
    bars = [i for i in (m.start() for m in re.finditer(r"\|\|", text)) if not any(a <= i < b for a, b in code)]
    return [(bars[i], bars[i + 1] + 2) for i in range(0, len(bars) - 1, 2)]


def in_spoiler(text: str, pos: int) -> bool:
    return any(a <= pos < b for a, b in spoiler_spans(text))


def fix_url(url: str, rule: dict, host: str) -> str:
    """url moved onto fixer host, keeping only whitelisted query params.
    The whole host is swapped (www.instagram.com -> ig.example.com, not www.ig.example.com)."""
    parsed = urllib.parse.urlparse(url)
    allowed = KEEP_PARAMS - set(rule.get("drop", []))
    clean_query = "&".join(
        f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query) if k in allowed
    )
    return urllib.parse.urlunparse(parsed._replace(scheme="https", netloc=host, query=clean_query, fragment=""))


def has_embed_media(html: str) -> bool:
    """True if the page gives Discord an image/video to embed. A fixer that
    couldn't fetch the post usually still answers 200, just without media tags."""
    if ACTIVITY_RE.search(html or ""):
        return True
    for tag in META_RE.findall(html or ""):
        attrs = {k.lower(): dq or sq for k, dq, sq in ATTR_RE.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name in EMBED_MEDIA_KEYS and (attrs.get("content") or "").strip():
            return True
    return False


def rewrite_links(text: str, links, chosen: dict) -> str:
    """Swap each link for chosen[url]; links without a working fixer stay as they were."""
    out, last = [], 0
    for start, end, url, _rule in links:
        if url in chosen:
            out.append(text[last:start])
            out.append(chosen[url])
            last = end
    out.append(text[last:])
    return "".join(out)


IG_POST_RE = re.compile(r"^/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def instagram_shortcode(url: str) -> str | None:
    """Post id from an Instagram post/reel URL, or None (profiles, stories, share links)."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname or not matches_domain(parsed.hostname, "instagram.com"):
        return None
    m = IG_POST_RE.match(parsed.path)
    return m.group(1) if m else None


X_STATUS_RE = re.compile(r"^/([\w]+)/status(?:es)?/(\d+)")
# X's lang values that aren't a language: und (unknown), zxx (no text), qme (media/links only),
# qam (mentions only), qht (hashtags only), qct (cashtags only), qst (very short), art (emoji/symbols)
NOT_A_LANGUAGE = {"", "und", "zxx", "qme", "qam", "qht", "qct", "qst", "art"}


HTML_LANG_RE = re.compile(r"<html\b[^>]*\blang\s*=\s*[\"']?([\w-]+)", re.IGNORECASE)


def page_lang(html: str) -> str | None:
    """The lang attribute of a page's <html> tag (FxEmbed sets it to the post's language)."""
    m = HTML_LANG_RE.search(html or "")
    return m.group(1) if m else None


CARD_RE = re.compile(r"""<meta\b[^>]*\b(?:name|property)\s*=\s*["']twitter:card["'][^>]*>""", re.IGNORECASE)


def page_media(html: str) -> str | None:
    """"video", "image" or None (text only) from a fixer page's twitter:card."""
    m = CARD_RE.search(html or "")
    attrs = {k.lower(): dq or sq for k, dq, sq in ATTR_RE.findall(m.group(0))} if m else {}
    card = attrs.get("content", "")
    return "video" if card == "player" else "image" if card == "summary_large_image" else None


def x_status(url: str) -> tuple[str, str] | None:
    """(user, status id) from an x.com/twitter.com post URL, or None."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not (matches_domain(host, "x.com") or matches_domain(host, "twitter.com")):
        return None
    m = X_STATUS_RE.match(parsed.path)
    return (m.group(1), m.group(2)) if m else None


def needs_translation(lang: str | None) -> bool:
    """True for a post language other than English (X's lang codes)."""
    lang = (lang or "").lower()
    return lang not in NOT_A_LANGUAGE and not lang.startswith("en")


def translated_x_url(url: str, host: str) -> str | None:
    """The post on an FxEmbed host with /en added, which makes FxEmbed translate it to English."""
    status = x_status(url)
    return f"https://{host}/{status[0]}/status/{status[1]}/en" if status else None


def instagram_url(url: str) -> str:
    """The post on instagram.com itself, minus tracking params (for the "Open on Instagram" button)."""
    parsed = urllib.parse.urlparse(url)
    query = "&".join(f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query) if k == "img_index")
    return urllib.parse.urlunparse(parsed._replace(scheme="https", netloc="www.instagram.com", query=query, fragment=""))


def open_button(url: str) -> tuple[str, str] | None:
    """(site, link) for a button that opens the post on its own site, for the sites that get one."""
    if instagram_shortcode(url):
        return "instagram", instagram_url(url)
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if (matches_domain(host, "x.com") or matches_domain(host, "twitter.com")) and "/status/" in parsed.path:
        return "x", urllib.parse.urlunparse(("https", "x.com", parsed.path, "", "", ""))
    return None


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def short_link(url: str) -> str:
    """Compact label for a link in status notices: host plus a short path, not clickable."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    path = parsed.path.rstrip("/")
    if len(path) > 30:
        path = path[:29] + "…"
    return f"`{host}{path}`"


def webhook_name(name: str) -> str:
    """Discord rejects webhook usernames containing "discord" or "clyde"."""
    name = re.sub(r"discord|clyde", "", name or "", flags=re.IGNORECASE).strip()[:80]
    return name or "someone"

def get_response(message:str) -> str:
	p_message = message.lower()

	if p_message.split()[0] in ["hello", "hey", "hi", "yo", "sup", "wassup", "hola"]:
		return "_Hello, there!_"
	
	elif p_message.split()[0] in  ["inspire", "motivate"] or "depressed" in p_message.split():
		return get_quote()
	
	else:
		return "Uhh, dk what you're saying, bruh."


def clip(text: str, limit: int) -> str:
    """text cut to at most limit characters, with "..." when cut."""
    return text if len(text) <= limit else text[:limit - 3] + "..."


# Single-song links that get buttons for the other platforms (albums and playlists don't)
MUSIC_DOMAINS = [{"domain": d} for d in ("open.spotify.com", "music.apple.com", "music.youtube.com")]
SPOTIFY_TRACK_RE = re.compile(r"^/(?:intl-[\w-]+/)?track/([A-Za-z0-9]+)")
APPLE_SONG_RE = re.compile(r"/song/(?:[^/]+/)?(\d+)")


def music_track(url: str) -> tuple[str, str] | None:
    """("spotify" | "apple" | "ytmusic", id) for a single-song link, else None."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    query = urllib.parse.parse_qs(parsed.query)
    if host == "open.spotify.com":
        m = SPOTIFY_TRACK_RE.match(parsed.path)
        return ("spotify", m.group(1)) if m else None
    if host == "music.apple.com":
        if query.get("i"):
            return "apple", query["i"][0]
        m = APPLE_SONG_RE.search(parsed.path)
        return ("apple", m.group(1)) if m else None
    if host == "music.youtube.com" and parsed.path == "/watch" and query.get("v"):
        return "ytmusic", query["v"][0]
    return None


def find_music_links(text: str) -> list[str]:
    """Single-song Spotify / Apple Music / YouTube Music links in text, same skipping rules as find_links."""
    return [url for _s, _e, url, _r in find_links(text, MUSIC_DOMAINS) if music_track(url)]


def music_buttons(url: str, platform: str, song: str, apple_url: str | None) -> list[tuple[str, str]]:
    """(platform, link) buttons for the song on the other platforms. Apple Music links straight to the
    song (found with Apple's search API); Spotify and YouTube Music open a search for it."""
    buttons = []
    if platform != "spotify":
        buttons.append(("spotify", f"https://open.spotify.com/search/{urllib.parse.quote(song)}"))
    if platform != "apple" and apple_url:
        buttons.append(("applemusic", apple_url))
    if platform != "ytmusic":
        buttons.append(("youtubemusic", f"https://music.youtube.com/search?q={urllib.parse.quote_plus(song)}"))
    platform_id = music_track(url)
    if platform_id and platform_id[0] == "spotify":
        url = f"https://open.spotify.com/track/{platform_id[1]}"  # without ?si= tracking
    elif platform_id and platform_id[0] == "ytmusic":
        url = f"https://music.youtube.com/watch?v={platform_id[1]}"
    buttons.append(("songlink", f"https://song.link/{url}"))
    return buttons


# "(Official Video)", "[4K]", "(Lyrics)" and similar video-title extras that spoil a song search
VIDEO_EXTRAS_RE = re.compile(
    r"\s*[(\[][^)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|4k|hd|mv|m/v)\b[^)\]]*[)\]]", re.IGNORECASE
)

TRAILING_EXTRAS_RE = re.compile(r"\s+official(\s+(music|lyric))?\s+(video|audio)\s*$", re.IGNORECASE)


def clean_song(name: str) -> str:
    """A video title tidied into a song search: extras dropped, "Artist - Title" dash removed."""
    name = TRAILING_EXTRAS_RE.sub("", VIDEO_EXTRAS_RE.sub("", name))
    return re.sub(r"\s+", " ", name.replace(" - ", " ")).strip()


def meta_content(html: str, key: str) -> str | None:
    """content of the <meta property|name=key> tag, unescaped."""
    for tag in META_RE.findall(html or ""):
        attrs = {k.lower(): dq or sq for k, dq, sq in ATTR_RE.findall(tag)}
        if (attrs.get("property") or attrs.get("name") or "").lower() == key:
            return html_lib.unescape(attrs.get("content") or "").strip() or None
    return None


def download_link(url: str, media: str | None, x_host: str, instafix: str, via: str | None = None) -> str | None:
    """Link that opens the post's media file itself (best quality), for the Download button.
    Both fixers resolve it fresh on every click, so the button keeps working after CDN links expire.
    via is the fixer link the repost uses: an Instagram post gets InstaFix's download only when
    InstaFix is what embedded it (when it fell back to another fixer, InstaFix can't get it)."""
    if media not in ("video", "image"):
        return None
    status = x_status(url)
    if status:
        # FxEmbed sends /status/<id>.mp4 to the post's first video or photo, whichever it has
        return f"https://{x_host}/{status[0]}/status/{status[1]}.mp4"
    code = instagram_shortcode(url)
    if code and instafix and (via is None or host_of(via) == host_of(instafix)):
        return f"{instafix}/{'videos' if media == 'video' else 'images'}/{code}/1"
    return None


def link_key(url: str) -> str:
    """Same key for every link to one post (x.com and twitter.com, tracking params, www.)."""
    status = x_status(url)
    if status:
        return f"x:{status[1]}"
    code = instagram_shortcode(url)
    if code:
        return f"ig:{code}"
    song = music_track(url)
    if song:
        return f"{song[0]}:{song[1]}"
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    query = "&".join(f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query) if k in KEEP_PARAMS)
    return f"{host}{parsed.path.rstrip('/')}" + (f"?{query}" if query else "")


class SeenLinks:
    """First repost of each post per server, so a later share can say "posted before" and link to it.
    Kept in SQLite next to config.json so it survives restarts and deploys."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS seen (
            guild_id INTEGER, key TEXT, message_id INTEGER, jump_url TEXT, author TEXT, posted_at INTEGER,
            PRIMARY KEY (guild_id, key))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS seen_message ON seen (message_id)")
        self.db.commit()

    def earlier(self, guild_id: int, keys) -> dict[str, tuple[str, str, int]]:
        """key -> (jump url, author name, unix time) for the keys posted here before."""
        keys = list(keys)
        if not keys:
            return {}
        rows = self.db.execute(
            f"SELECT key, jump_url, author, posted_at FROM seen WHERE guild_id = ? AND key IN ({','.join('?' * len(keys))})",
            [guild_id, *keys],
        )
        return {key: (jump, author, posted_at) for key, jump, author, posted_at in rows}

    def remember(self, guild_id: int, keys, message_id: int, jump_url: str, author: str):
        """Keeps the first post of each key; later reposts don't replace it."""
        now = int(time.time())
        self.db.executemany(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?, ?)",
            [(guild_id, key, message_id, jump_url, author, now) for key in keys],
        )
        self.db.commit()

    def forget(self, message_id: int):
        self.db.execute("DELETE FROM seen WHERE message_id = ?", (message_id,))
        self.db.commit()
