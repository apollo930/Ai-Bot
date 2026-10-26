import sqlite3

import utils

IG = {"domain": "instagram.com", "replacement": ["ig.reaper113.xyz", "kkinstagram.com"]}
X = {"domain": "x.com", "replacement": "fixupx.com"}
THREADS = {"domain": "www.threads.com", "replacement": "fixthreads.seria.moe"}
RULES = [IG, X, THREADS]


def test_rule_hosts_accepts_old_and_new_formats():
    assert utils.rule_hosts(IG) == ["ig.reaper113.xyz", "kkinstagram.com"]
    assert utils.rule_hosts({"replacement": "a.com, b.com"}) == ["a.com", "b.com"]


def test_matches_domain():
    assert utils.matches_domain("www.instagram.com", "instagram.com")
    assert utils.matches_domain("instagram.com", "instagram.com")
    assert not utils.matches_domain("notinstagram.com", "instagram.com")
    assert not utils.matches_domain("scontent.cdninstagram.com", "instagram.com")
    assert utils.matches_domain("threads.com", "www.threads.com")


def test_find_links_skips_suppressed_code_and_bare_domains():
    text = "a https://www.instagram.com/p/AAA/ b <https://x.com/u/status/1> `https://x.com/u/status/2` https://x.com/ https://x.com/u/status/3."
    found = [(url, rule["domain"]) for _s, _e, url, rule in utils.find_links(text, RULES)]
    assert found == [("https://www.instagram.com/p/AAA/", "instagram.com"), ("https://x.com/u/status/3", "x.com")]


def test_find_links_stops_at_spoiler_bars_and_parens():
    text = "||https://x.com/u/status/1|| (https://x.com/u/status/2)"
    assert [url for _s, _e, url, _r in utils.find_links(text, RULES)] == ["https://x.com/u/status/1", "https://x.com/u/status/2"]


def test_rewrite_links_keeps_text_and_unfixed_links():
    text = "see https://x.com/u/status/1 and https://www.instagram.com/p/AAA/!"
    links = utils.find_links(text, RULES)
    out = utils.rewrite_links(text, links, {"https://x.com/u/status/1": "https://fixupx.com/u/status/1"})
    assert out == "see https://fixupx.com/u/status/1 and https://www.instagram.com/p/AAA/!"


def test_fix_url_swaps_host_and_drops_tracking():
    assert utils.fix_url("https://www.instagram.com/p/AAA/?igsh=xyz&img_index=2", IG, "ig.reaper113.xyz") == "https://ig.reaper113.xyz/p/AAA/?img_index=2"
    assert utils.fix_url("https://youtube.com/watch?v=abc&si=track", {}, "h") == "https://h/watch?v=abc"


def test_spoilers():
    t = "look ||https://x.com/u/status/1|| lol"
    assert utils.in_spoiler(t, t.index("https"))
    assert not utils.in_spoiler("https://x.com/u/status/1 ||spoiler||", 0)
    t = "||a|| https://x.com/u/status/1 ||b||"
    assert not utils.in_spoiler(t, t.index("https"))
    t = "`||` ||https://x.com/u/status/1||"  # bars in code don't pair
    assert utils.in_spoiler(t, t.index("https"))
    assert not utils.in_spoiler("||unclosed https://x.com/u/status/1", 11)


def test_has_embed_media():
    assert utils.has_embed_media('<meta property="og:image" content="https://a/b.jpg">')
    assert utils.has_embed_media('<link rel="alternate" type="application/activity+json" href="https://x">')
    assert not utils.has_embed_media('<meta property="og:image" content="">')
    assert not utils.has_embed_media('<meta property="og:title" content="Sorry, that post doesn\'t exist">')


def test_page_media_and_lang():
    assert utils.page_media('<meta name="twitter:card" content="player">') == "video"
    assert utils.page_media("<meta property='twitter:card' content='summary_large_image'>") == "image"
    assert utils.page_media('<meta name="twitter:card" content="summary">') is None
    assert utils.page_lang('<html lang="ja"><head>') == "ja"
    assert utils.page_lang("<p>no html tag</p>") is None


def test_instagram_and_x_ids():
    assert utils.instagram_shortcode("https://www.instagram.com/reel/DbL0D7FIfgh/") == "DbL0D7FIfgh"
    assert utils.instagram_shortcode("https://instagram.com/someuser/p/AAA/") == "AAA"
    assert utils.instagram_shortcode("https://www.instagram.com/someuser/") is None
    assert utils.x_status("https://twitter.com/jack/status/20?s=1") == ("jack", "20")
    assert utils.x_status("https://fixupx.com/jack/status/20") is None


def test_translation():
    assert utils.needs_translation("ja")
    for lang in ("en", "en-gb", "und", "qme", "", None):
        assert not utils.needs_translation(lang)
    assert utils.translated_x_url("https://x.com/u/status/5", "x.reaper113.xyz") == "https://x.reaper113.xyz/u/status/5/en"


def test_open_button():
    assert utils.open_button("https://instagram.com/p/AAA/?igsh=z") == ("instagram", "https://www.instagram.com/p/AAA/")
    assert utils.open_button("https://twitter.com/u/status/5?s=20") == ("x", "https://x.com/u/status/5")
    assert utils.open_button("https://reddit.com/r/a") is None


def test_download_link():
    fx, ig = "x.reaper113.xyz", "https://ig.reaper113.xyz"
    assert utils.download_link("https://x.com/u/status/5", "video", fx, ig) == "https://x.reaper113.xyz/u/status/5.mp4"
    assert utils.download_link("https://x.com/u/status/5", None, fx, ig) is None
    url = "https://www.instagram.com/p/AAA/"
    assert utils.download_link(url, "image", fx, ig, "https://ig.reaper113.xyz/p/AAA/") == "https://ig.reaper113.xyz/images/AAA/1"
    # fell back to another fixer: InstaFix couldn't get it, so no InstaFix download
    assert utils.download_link(url, "video", fx, ig, "https://vxinstagram.com/p/AAA/") is None


def test_link_key_groups_the_same_post():
    assert utils.link_key("https://twitter.com/a/status/9") == utils.link_key("https://x.com/b/status/9?s=1") == "x:9"
    assert utils.link_key("https://www.instagram.com/p/AAA/?igsh=1") == "ig:AAA"
    assert utils.link_key("https://open.spotify.com/track/abc?si=1") == "spotify:abc"
    assert utils.link_key("https://m.example.com/a/?utm=1") == "example.com/a"


def test_music():
    assert utils.music_track("https://open.spotify.com/intl-de/track/abc?si=1") == ("spotify", "abc")
    assert utils.music_track("https://music.apple.com/in/album/x/1?i=123") == ("apple", "123")
    assert utils.music_track("https://music.youtube.com/watch?v=vid&list=x") == ("ytmusic", "vid")
    assert utils.music_track("https://open.spotify.com/album/abc") is None
    buttons = dict(utils.music_buttons("https://open.spotify.com/track/abc?si=1", "spotify", "A B", "https://music.apple.com/s/1"))
    assert "spotify" not in buttons and buttons["applemusic"] == "https://music.apple.com/s/1"
    assert buttons["songlink"] == "https://song.link/https://open.spotify.com/track/abc"
    assert utils.clean_song("Artist - Song (Official Music Video) [4K]") == "Artist Song"


def test_reaction_helpers():
    assert utils.reaction_extension("Doakes Stare.GIF") == ".gif"
    assert utils.reaction_extension("https://cdn.discordapp.com/attachments/1/2/clip.mp4?ex=1") == ".mp4"
    assert utils.reaction_extension("https://tenor.com/view/thing-123") == ".gif"
    assert utils.is_discord_upload("https://cdn.discordapp.com/attachments/1/2/a.gif?ex=1")
    assert utils.is_discord_upload("https://media.discordapp.net/attachments/1/2/a.gif")
    assert not utils.is_discord_upload("https://cdn.discordapp.com/emojis/1.png")
    assert not utils.is_discord_upload("https://media.tenor.com/a.gif")


def test_fit_items():
    assert utils.fit_items(["a", "b"], 100) == "a · b"
    out = utils.fit_items([f"`name_{i:03d}`" for i in range(500)], 4000)
    assert len(out) <= 4000 and out.endswith("more")
    assert utils.fit_items(["x" * 50], 20) == "…and 1 more"


def test_webhook_name():
    assert utils.webhook_name("Discord Fan") == "Fan"
    assert utils.webhook_name("clyde") == "someone"


def test_seen_links_and_repost_owners(tmp_path):
    db = str(tmp_path / "links.db")
    seen = utils.SeenLinks(db)
    seen.remember(1, ["x:9"], 100, "https://discord.com/channels/1/2/100", "VB")
    seen.remember(1, ["x:9"], 200, "https://discord.com/channels/1/2/200", "Other")  # first one is kept
    assert seen.earlier(1, ["x:9", "ig:A"])["x:9"][:2] == ("https://discord.com/channels/1/2/100", "VB")
    assert seen.earlier(2, ["x:9"]) == {}
    seen.forget(100)
    assert seen.earlier(1, ["x:9"]) == {}

    owners = utils.RepostOwners(db)
    owners.add(100, 42)
    assert owners.owner(100) == 42 and owners.owner(101) is None
    owners.forget(100)
    assert owners.owner(100) is None
    owners.KEEP = 3
    for mid in range(10):
        owners.add(mid, 1)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM reposts").fetchone()[0] == 3


def test_x_media_plan_picks_videos_that_fit():
    tweet = {"media": {
        "all": [{"type": "video", "url": "v"}, {"type": "photo", "url": "p.jpg"}, {"type": "photo", "url": "q.jpg"}],
        "videos": [{"url": "v", "duration": 10, "formats": [
            {"url": "hls", "container": "m3u8"},
            {"url": "360p", "container": "mp4", "bitrate": "800000"},
            {"url": "1080p", "container": "mp4", "bitrate": "10000000"},
            {"url": "720p", "container": "mp4", "bitrate": "2000000"},
        ]}],
    }}
    # 10 s at 10 Mbit/s is about 12.5 MB, too big for 10 MB; 720p (~2.5 MB) fits
    assert utils.x_media_plan(tweet, 10 * 2**20, 10) == [("video", ["720p", "360p"]), ("image", ["p.jpg"]), ("image", ["q.jpg"])]
    assert utils.x_media_plan(tweet, 10 * 2**20, 1) == [("video", ["720p", "360p"])]
    assert utils.x_media_plan(tweet, 100_000, 10)[0] == ("image", ["p.jpg"])  # no MP4 fits: video left out
    no_duration = {"media": {"all": [{"type": "gif", "url": "g"}], "videos": [{"url": "g", "formats": [{"url": "a", "container": "mp4", "bitrate": "1"}, {"url": "b", "container": "mp4", "bitrate": "9"}]}]}}
    assert utils.x_media_plan(no_duration, 10, 10) == [("video", ["b", "a"])]
    assert utils.x_media_plan({}, 10, 10) == []
