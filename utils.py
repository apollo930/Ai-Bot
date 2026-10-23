import random, requests, json, os, re, urllib.parse
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


def instagram_url(url: str) -> str:
    """The post on instagram.com itself, minus tracking params (for the "Open on Instagram" button)."""
    parsed = urllib.parse.urlparse(url)
    query = "&".join(f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query) if k == "img_index")
    return urllib.parse.urlunparse(parsed._replace(scheme="https", netloc="www.instagram.com", query=query, fragment=""))


def open_button(url: str) -> tuple[str, str] | None:
    """(label, link) for a button that opens the post on its own site, for the sites that get one."""
    if instagram_shortcode(url):
        return "Open on Instagram", instagram_url(url)
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if (matches_domain(host, "x.com") or matches_domain(host, "twitter.com")) and "/status/" in parsed.path:
        return "Open on X", urllib.parse.urlunparse(("https", "x.com", parsed.path, "", "", ""))
    return None


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
