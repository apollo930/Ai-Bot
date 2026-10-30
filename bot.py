import discord, os, utils, json, asyncio, time, aiohttp, io, glob, re, sys, tempfile
from discord.ext import tasks
from dotenv import load_dotenv

async def send_message(message, user_message, is_private):
	try:
		response=utils.get_response(user_message)
		await message.author.send(response) if is_private else await message.channel.send(response)

	except Exception as e:
		print("Error: " + str(e))


WORKING, FAILED = "⏳", "⚠️"  # reactions on the poster's message while fixing / when nothing could be fixed
SHOW_WORKING_AFTER = 1.5  # seconds: fast fixes never flash a reaction
DETAILS_KEPT = 300  # fixes whose details "Apps → Embed details" can still show
# Link buttons show only an emoji: the logo from emojis/btn_<kind>.png, or these until it's uploaded
PLAIN_BUTTON_EMOJI = {
	"instagram": "📸", "x": "✖️", "download": "⬇️",
	"spotify": "🟢", "applemusic": "🍎", "youtubemusic": "▶️", "songlink": "🔗",
}
# Saved reactions (/addai): uploaded files live here (gitignored; the bots' failover syncs data/)
REACTIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "reactions")
REACTION_MAX_BYTES = 10 * 2**20  # Discord's upload limit in a server without boosts
REACTION_NAME_RE = re.compile(r"[\w-]{1,32}")
REACTION_TRIGGER_RE = re.compile(r";([\w-]{1,32})(?:\s+(<@!?\d+>))?")  # ;name, optionally with one @mention
FALLBACK_TIMEOUT = 60  # seconds yt-dlp gets to fetch a post's video when no fixer embeds it
DEAD_STATUSES = {404, 410, 451}  # gone for good (403 isn't: some sites refuse bots but work for people)


def run_discord_bot():

		intents = discord.Intents.default()
		intents.message_content = True
		intents.presences = True
		intents.members = True
		client = discord.Client(intents=intents)
		tree = discord.app_commands.CommandTree(client)

		@tasks.loop(seconds=60)
		async def update_presence():
			cpu_temp = utils.get_cpu_temp()
			await client.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"your feelings @{cpu_temp}"))

		@client.event
		async def on_ready():
			nonlocal game_log_channel, url_rules, reactions
			cpu_temp = utils.get_cpu_temp()
			await client.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"your feelings @{cpu_temp}"))
			update_presence.start()
			if not check_reactions.is_running():
				check_reactions.start()
			for guild in client.guilds:
				if guild.system_channel:
					await guild.system_channel.send("Bot is online!")
			try:
				with open("config.json") as f:
					data = json.load(f)
					ch_id = data.get("game_log_channel_id")
					if ch_id:
						ch = client.get_channel(ch_id)
						if ch and ch.permissions_for(ch.guild.me).send_messages:
							game_log_channel = ch
							print(f"Game log channel set to #{ch.name}", flush=True)
						else:
							print(f"Warning: config.json channel {ch_id} not found or not messageable", flush=True)
					else:
						print("No game_log_channel_id in config.json", flush=True)
					url_rules.clear()
					url_rules.extend(data.get("url_rules", []))
					reactions.clear()
					reactions.update(data.get("reactions", {}))
			except FileNotFoundError:
				print("No config.json found — use /setgamechannel to configure game tracking", flush=True)
			try:
				if GUILD_ID:
					g = discord.Object(id=int(GUILD_ID))
					tree.clear_commands(guild=g)
					await tree.sync(guild=g)
					tree.copy_global_to(guild=g)
					synced = await tree.sync(guild=g)
				else:
					synced = await tree.sync()
				print(f"Synced {len(synced)} command(s)", flush=True)
			except Exception as e:
				print(f"Slash command sync failed: {e}", flush=True)
			await load_button_emojis()
			print(f"{client.user} is ready!", flush=True)

		async def load_button_emojis():
			"""Logo emojis for the link buttons: emojis/*.png, uploaded once as the bot's own
			application emojis (usable in every server). Missing ones fall back to plain emojis."""
			try:
				have = {e.name: e for e in await client.fetch_application_emojis()}
				for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "emojis", "*.png"))):
					name = os.path.splitext(os.path.basename(path))[0]
					if name not in have:
						with open(path, "rb") as f:
							have[name] = await client.create_application_emoji(name=name, image=f.read())
						print(f"Uploaded button emoji :{name}:", flush=True)
				app_emojis.update(have)
			except Exception as e:
				print(f"Button emojis unavailable, using plain ones: {e}", flush=True)

		def button_emoji(kind: str):
			return app_emojis.get(f"btn_{kind}") or PLAIN_BUTTON_EMOJI[kind]

		async def read_page(resp, limit: int = 256 * 1024) -> str:
			"""The first `limit` bytes of a page. resp.content.read(n) only returns what has arrived
			so far, and some sites (vxreddit) send the page in pieces, so this reads on until the limit."""
			body = bytearray()
			async for chunk in resp.content.iter_chunked(64 * 1024):
				body += chunk
				if len(body) >= limit:
					break
			return bytes(body[:limit]).decode("utf-8", errors="replace")

		async def embed_problem(url: str, page: dict | None = None, origin: str | None = None) -> str | None:
			"""None if Discord would get a media embed from this fixer URL, else what went wrong.
			page, when given, gets the post's language as the fixer page states it (FxEmbed's <html lang>)
			and what media the post has ("video", "image" or None, for the Download button).
			origin is the site the link came from (the rule's domain)."""
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(url, timeout=aiohttp.ClientTimeout(total=10), max_redirects=5) as resp:
					# A fixer that can't get the post often redirects to the post itself. Instagram then
					# serves its login page, whose og:image would pass the media check below.
					if origin and resp.url.host and utils.matches_domain(resp.url.host, origin):
						return f"sent back to {resp.url.host} (it couldn't get the post)"
					if resp.status != 200:
						return f"HTTP {resp.status}"
					# kkinstagram-style fixers redirect Discordbot straight to the media file
					if resp.content_type.startswith(("image/", "video/")):
						if page is not None:
							page["media"] = resp.content_type.split("/")[0]
						return None
					if "html" not in resp.content_type:
						return f"answered with {resp.content_type}"
					html = await read_page(resp)
					error = utils.fixer_error(html)
					if error:
						return f"it couldn't get the post ({utils.clip(error, 80)})"
					if page is not None:
						page["lang"] = utils.page_lang(html)
						page["media"] = utils.page_media(html)
						page["images"] = utils.page_images(html)
					if utils.has_embed_media(html):
						return None
					return "no image/video in its page (post not found or blocked)"
			except TimeoutError:
				return "timed out (10s)"
			except Exception as e:
				print(f"Probe failed for {url}: {e.__class__.__name__}", flush=True)
				return f"couldn't connect ({e.__class__.__name__})"

		async def resolve(url: str, rule: dict, steps: list) -> tuple[str | None, str | None]:
			"""(first fixer URL for url that embeds, its media kind), trying the rule's hosts in order.
			(None, utils.KEEP_ORIGINAL) when the original link's own embed is the better one for this
			post (see utils.FIXER_ONLY_FOR). Each attempt goes into steps, for "Apps → Embed details"."""
			hit = fixed_cache.get(url)
			if hit and time.time() - hit[0] < 600:
				if hit[1] is None:
					steps.append("✓ kept the original link: decided for this link in the last 10 min")
				else:
					steps.append(f"✓ `{utils.host_of(hit[1])}`: worked for this link in the last 10 min, reused")
				return hit[1], hit[2]
			only = utils.fixer_only_for(rule)

			def remember(fixed, media):
				if len(fixed_cache) > 500:
					fixed_cache.clear()
				fixed_cache[url] = (time.time(), fixed, media)
			candidates = [(host, utils.fix_url(url, rule, host), "") for host in utils.rule_hosts(rule)]
			checked, pages = {}, {}
			if utils.x_status(url) and candidates:
				# Our FxEmbed translates through the private translator, so non-English posts go
				# there first. FxEmbed fixers (fixupx, ours) state the post's language in their page.
				first = candidates[0][1]
				pages[first] = {}
				checked[first] = await embed_problem(first, pages[first], rule.get("domain"))
				lang = pages[first].get("lang")
				if utils.needs_translation(lang):
					candidates.insert(0, (FXEMBED_HOST, utils.translated_x_url(url, FXEMBED_HOST), f", translated from `{lang}`"))
			for host, candidate, note in candidates:
				if candidate not in checked:
					pages[candidate] = {}
					checked[candidate] = await embed_problem(candidate, pages[candidate], rule.get("domain"))
				problem = checked[candidate]
				# a page that loaded but shows no media is a text post on vxreddit, not a failure
				if only is not None and (problem is None or "media" in pages[candidate]):
					kind = utils.post_kind(pages[candidate].get("media"), pages[candidate].get("images", 0))
					if kind not in only:
						steps.append(f"✓ kept the original link: its own embed is better for this {kind or 'text/link'} post (`{host}` is only used for {' and '.join(sorted(only))})")
						remember(None, utils.KEEP_ORIGINAL)
						return None, utils.KEEP_ORIGINAL
				if problem is None:
					steps.append(f"✓ `{host}`: used{note}")
					media = pages[candidate].get("media")
					remember(candidate, media)
					return candidate, media
				steps.append(f"✗ `{host}`: {problem}")
				print(f"Fixer {host} has no embed for {url}: {problem}", flush=True)
			if only is not None:
				# the site's own embed still works (a card at least), so the link stays as it is
				steps.append("✓ kept the original link: its own embed works without a fixer")
				return None, utils.KEEP_ORIGINAL
			return None, None

		async def download_video(url: str, max_bytes: int, spoiler: bool, steps: list) -> discord.File | None:
			"""Last resort when no fixer embeds a post: fetch its video with yt-dlp (a separate process,
			at most max_bytes and FALLBACK_TIMEOUT seconds) to attach to the repost."""
			if max_bytes <= 0:
				return None
			with tempfile.TemporaryDirectory() as tmp:
				proc = await asyncio.create_subprocess_exec(
					sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist",
					"--max-filesize", str(max_bytes),
					"-f", f"b[ext=mp4][filesize<{max_bytes}]/b[filesize<{max_bytes}]/b[ext=mp4]/b",
					"-o", os.path.join(tmp, "video.%(ext)s"), "--print", "after_move:filepath", url,
					stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
				)
				try:
					out, err = await asyncio.wait_for(proc.communicate(), FALLBACK_TIMEOUT)
				except TimeoutError:
					proc.kill()
					await proc.wait()
					steps.append(f"✗ download fallback: timed out ({FALLBACK_TIMEOUT}s)")
					return None
				lines = out.decode(errors="replace").strip().splitlines()
				path = lines[-1] if lines else ""
				if proc.returncode != 0 or not path or not os.path.isfile(path):
					why = (err.decode(errors="replace").strip().splitlines() or ["no video, or over the upload limit"])[-1]
					steps.append(f"✗ download fallback: {utils.clip(why.removeprefix('ERROR: '), 150)}")
					return None
				size = os.path.getsize(path)
				if size > max_bytes:
					steps.append("✗ download fallback: the video is over this server's upload limit")
					return None
				with open(path, "rb") as f:
					data = f.read()
			steps.append(f"📥 no fixer worked, so the video was downloaded and attached ({size / 2**20:.1f} MB, yt-dlp)")
			name = utils.link_key(url).replace(":", "_").replace("/", "_")[:60]
			return discord.File(io.BytesIO(data), filename=f"{name}{os.path.splitext(path)[1]}", spoiler=spoiler)

		async def fetch_file(urls: list[str], size_limit: int, filename: str, spoiler: bool) -> discord.File | None:
			"""The first of urls that downloads within size_limit, as a file named filename + its extension."""
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			for url in urls:
				try:
					async with http.get(url, timeout=aiohttp.ClientTimeout(total=40)) as resp:
						if resp.status != 200 or (resp.content_length or 0) > size_limit:
							continue
						data = await resp.read()
						if len(data) > size_limit:
							continue
						ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4"}.get(resp.content_type, ".mp4" if "video" in resp.content_type else ".jpg")
						return discord.File(io.BytesIO(data), filename=f"{filename}{ext}", spoiler=spoiler)
				except Exception as e:
					print(f"Download failed for {url}: {e.__class__.__name__}", flush=True)
			return None

		async def x_media(url: str, room: int, size_limit: int):
			"""For an X link inside spoiler bars (Discord doesn't preview those): (files, embed, total)
			with the post's photos and videos from our FxEmbed's API, spoilered, so they can be
			revealed with a click. Each video is the best MP4 that fits the upload limit. None if
			it's not an X post, has no media, or nothing could be fetched."""
			status = utils.x_status(url)
			if not status or room <= 0:
				return None
			tweet = ((await get_json(f"{FXEMBED_API}/status/{status[1]}", {})) or {}).get("tweet") or {}
			plan = utils.x_media_plan(tweet, size_limit, room)
			if not plan:
				return None
			got = await asyncio.gather(*(fetch_file(urls, size_limit, f"x_{status[1]}_{i + 1}", True) for i, (_kind, urls) in enumerate(plan)))
			files = [f for f in got if f]
			if not files:
				return None
			author = tweet.get("author") or {}
			embed = discord.Embed(url=f"https://x.com/{status[0]}/status/{status[1]}", color=0x000000)
			if author.get("screen_name"):
				embed.set_author(name=f"{author.get('name') or author['screen_name']} (@{author['screen_name']})", url=f"https://x.com/{author['screen_name']}")
			if tweet.get("text"):
				embed.description = f"||{utils.clip(tweet['text'], 300)}||"
			total = len((tweet.get("media") or {}).get("all") or [])
			if total > len(files):
				embed.set_footer(text=f"+ {total - len(files)} more on X")
			return files, embed, total

		async def instagram_gallery(url: str, room: int, size_limit: int, spoiler: bool = False):
			"""For an Instagram carousel: (files, embed, total items), so the repost carries the
			real photos and videos, in order, as a Discord gallery instead of one grid image
			(videos play inline). Items over the upload limit are left out and counted in the
			footer. With spoiler (the link was inside ||spoiler bars||) the files are spoilered and so
			is the caption, and single posts get this too (Discord doesn't preview a spoilered link).
			None for single photos and reels otherwise, or when INSTAFIX_API isn't set."""
			code = utils.instagram_shortcode(url)
			if not INSTAFIX_API or not code or room <= 0:
				return None
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(f"{INSTAFIX_API}/api/p/{code}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
					if resp.status != 200:
						return None
					post = await resp.json(content_type=None)
			except Exception as e:
				print(f"InstaFix API failed for {code}: {e.__class__.__name__}", flush=True)
				return None
			media = [m for m in post.get("media") or [] if m.get("type") in ("image", "video") and m.get("url")]
			# single posts too when spoilered: Discord doesn't preview a link inside spoiler bars
			if len(media) < (1 if spoiler else 2):
				return None
			exts = {"image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4"}

			async def download(i, item):
				video = item["type"] == "video"
				try:
					async with http.get(item["url"], timeout=aiohttp.ClientTimeout(total=40 if video else 20)) as resp:
						if resp.status != 200 or (resp.content_length or 0) > size_limit:
							return None
						data = await resp.read()
						if len(data) > size_limit:
							return None
						ext = exts.get(resp.content_type, ".mp4" if video else ".jpg")
						return discord.File(io.BytesIO(data), filename=f"{code}_{i + 1}{ext}", spoiler=spoiler)
				except Exception as e:
					print(f"{item['type'].title()} download failed for {code}: {e.__class__.__name__}", flush=True)
					return None

			got = await asyncio.gather(*(download(i, m) for i, m in enumerate(media[:room])))
			files = [f for f in got if f]
			if not files:
				return None
			embed = discord.Embed(url=utils.instagram_url(url), color=0xE1306C)
			if post.get("username"):
				embed.set_author(name=f"@{post['username']}", url=f"https://www.instagram.com/{post['username']}/")
			caption = (post.get("caption") or "").strip()
			translated = (post.get("caption_translated") or "").strip()
			if translated:
				# InstaFix adds the English translation when the caption is in another language
				lang = post.get("caption_lang_name") or post.get("caption_lang")
				embed.description = (
					f"📑 Translated from {lang}\n\n{utils.clip(translated, 500)}"
					f"\n\n**Original:** {utils.clip(caption, 250)}"
				)
			elif caption:
				embed.description = utils.clip(caption, 300)
			if spoiler and embed.description:
				embed.description = f"||{embed.description}||"
			# what isn't attached: too big to upload, failed to download, or past the 10-file cap
			missing = [m["type"] for m, f in zip(media, got, strict=False) if not f] + [m["type"] for m in media[room:]]
			parts = []
			for kind, word in (("image", "photo"), ("video", "video")):
				n = missing.count(kind)
				if n:
					parts.append(f"{n} {word}{'s' if n > 1 else ''}")
			if parts:
				embed.set_footer(text=f"+ {' and '.join(parts)} on Instagram")
			return files, embed, len(media)

		async def get_json(endpoint: str, params: dict):
			"""GET endpoint?params and parse JSON (whatever its content type), or None."""
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(endpoint, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
					return await resp.json(content_type=None) if resp.status == 200 else None
			except Exception as e:
				print(f"Request failed for {endpoint}: {e.__class__.__name__}", flush=True)
				return None

		async def song_name(platform: str, sid: str) -> str | None:
			"""'Artist Title' for a song, from the platform's own public metadata."""
			nonlocal http
			if platform == "apple":
				data = await get_json("https://itunes.apple.com/lookup", {"id": sid, "country": APPLE_COUNTRY})
				hit = (data or {}).get("results") or []
				return f"{hit[0].get('artistName', '')} {hit[0].get('trackName', '')}".strip() if hit else None
			if platform == "ytmusic":
				data = await get_json("https://www.youtube.com/oembed", {"url": f"https://www.youtube.com/watch?v={sid}", "format": "json"})
				if not data:
					return None
				artist = (data.get("author_name") or "").removesuffix(" - Topic")
				title = data.get("title") or ""
				return utils.clean_song(title if artist.lower() in title.lower() else f"{artist} {title}")
			# Spotify: its track page names the song (og:title) and the artist (music:musician_description)
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(f"https://open.spotify.com/track/{sid}", timeout=aiohttp.ClientTimeout(total=8)) as resp:
					html = await read_page(resp) if resp.status == 200 else ""
			except Exception as e:
				print(f"Spotify page failed for {sid}: {e.__class__.__name__}", flush=True)
				return None
			title = utils.meta_content(html, "og:title")
			artist = utils.meta_content(html, "music:musician_description") or ""
			return f"{artist} {title}".strip() if title else None

		async def music_links(url: str, steps: list) -> list[tuple[str, str]] | None:
			"""Buttons for a single-song link on the other platforms, or None if the song can't be named."""
			platform, sid = utils.music_track(url)
			song = await song_name(platform, sid)
			if not song:
				steps.append("✗ 🎵 couldn't read the song's name, so no buttons for other platforms")
				return None
			apple_url = None
			if platform != "apple":
				data = await get_json("https://itunes.apple.com/search", {"term": song, "entity": "song", "limit": 1, "country": APPLE_COUNTRY})
				hit = (data or {}).get("results") or []
				apple_url = hit[0].get("trackViewUrl") if hit else None
			steps.append(f"🎵 “{song}”: buttons for the other platforms{'' if apple_url or platform == 'apple' else ' (not found on Apple Music)'}")
			return utils.music_buttons(url, platform, song, apple_url)

		async def fix_links(message, user_message):
			links = utils.find_links(user_message, url_rules)[:5]
			music = utils.find_music_links(user_message)[:2]
			if not (links or music) or message.flags.suppress_embeds:
				return
			limit = message.guild.filesize_limit
			if sum(a.size for a in message.attachments) > limit:
				return
			unique = {url: rule for _s, _e, url, rule in links}
			steps = {u: [] for u in [*unique, *music]}
			marker = asyncio.create_task(mark_working(message))
			try:
				fixed_any, sent = await repost_fixed(message, user_message, links, unique, limit, steps, music)
			finally:
				marker.cancel()
			remember_details(steps, message.id, sent.id if sent else None)
			if not fixed_any and not unique:
				await set_reaction(message, None)  # only song links, and none could be named: leave it be
			elif not fixed_any:
				await set_reaction(message, FAILED)  # left as is; Apps → Embed details says why
			elif sent is None:
				await set_reaction(message, None)  # nothing was reposted, so the original stays: clear ⏳

		async def mark_working(message):
			"""⏳ on the poster's message, only if fixing takes a moment."""
			await asyncio.sleep(SHOW_WORKING_AFTER)
			try:
				await message.add_reaction(WORKING)
			except discord.HTTPException:
				pass

		async def set_reaction(message, emoji):
			try:
				await message.remove_reaction(WORKING, client.user)
				if emoji:
					await message.add_reaction(emoji)
			except discord.HTTPException:
				pass

		def remember_details(steps, *message_ids):
			for mid in message_ids:
				if mid:
					fix_details[mid] = steps
			while len(fix_details) > DETAILS_KEPT:
				fix_details.pop(next(iter(fix_details)))

		async def repost_fixed(message, user_message, links, unique, limit, steps, music):
			"""Repost with fixed links and buttons for song links. Returns (fixed_any, the repost or None)."""
			results, songs = await asyncio.gather(
				asyncio.gather(*(resolve(u, r, steps[u]) for u, r in unique.items())),
				asyncio.gather(*(music_links(u, steps[u]) for u in music)),
			)
			song_buttons = {u: b for u, b in zip(music, songs) if b}
			chosen = {u: fixed for u, (fixed, _media) in zip(unique, results) if fixed}
			media = {u: kind for u, (_fixed, kind) in zip(unique, results)}
			# links whose own embed is the better one (Reddit text, link and photo posts) stay as they are
			kept = {u for u, kind in media.items() if kind == utils.KEEP_ORIGINAL}

			# One multi-photo Instagram post per message becomes a real gallery (attachments cap at 10).
			# A link inside ||spoiler bars|| gets spoilered files, or the photos would show openly.
			spoilered = {url for start, _e, url, _r in links if utils.in_spoiler(user_message, start)}
			# Discord doesn't preview a link inside spoiler bars, so a spoilered post's media is attached
			# (spoilered) for any Instagram or X post, not just carousels.
			gallery, gallery_url = None, None
			for url in unique:
				source = INSTAFIX_API
				gallery = await instagram_gallery(url, 10 - len(message.attachments), limit, url in spoilered)
				if not gallery and url in spoilered:
					source = FXEMBED_API
					gallery = await x_media(url, 10 - len(message.attachments), limit)
				if gallery:
					gallery_url = url
					steps[url].append(f"📷 attached {len(gallery[0])} of {gallery[2]} photos/videos (via `{utils.host_of(source)}`)")
					# the media is attached, so the link itself shouldn't embed too: <> stops Discord
					# previewing it, and it shows our fixer's address like every other repost
					chosen[url] = f"<{utils.fix_url(url, {}, utils.host_of(INSTAFIX_API))}>" if source == INSTAFIX_API else f"<{chosen.get(url, url)}>"
					break
			# Last resort for a post no fixer could embed: attach its video (one per message, to stay quick).
			# The link is then kept from embedding, since all it would show is a login page or an error.
			room = limit - sum(a.size for a in message.attachments)
			fallback = None
			for url in unique:
				# also for a spoilered link no gallery covered: its preview wouldn't show anyway
				if not gallery and url not in kept and (url not in chosen or url in spoilered):
					fallback = await download_video(url, room, url in spoilered, steps[url])
					if fallback:
						chosen[url] = f"<{chosen.get(url, url)}>"
						break
			if not chosen and not song_buttons:
				# nothing to change: every link keeps its own embed (no ❌), or no fixer works right now
				return len(kept) == len(unique), None
			transformed = utils.rewrite_links(user_message, links, chosen)
			# 🔁 for posts someone already shared here, linking to that first repost
			keys = {utils.link_key(u) for u in [*chosen, *song_buttons]}
			for key, (jump_url, name, posted_at) in seen_links.earlier(message.guild.id, keys).items():
				# [text](<url>): a bare message link makes Discord preview the whole earlier repost (embed and all)
				transformed += f"\n-# 🔁 [posted before](<{jump_url}>) by {discord.utils.escape_markdown(name)} <t:{posted_at}:R>"
			ref = message.reference.resolved if message.reference else None
			if isinstance(ref, discord.Message):
				transformed = f"-# ↪ replying to [{ref.author.display_name}]({ref.jump_url})\n{transformed}"
			if len(transformed) > 2000:
				steps[next(iter(steps))].append("✗ the fixed message would be over 2000 characters, so it wasn't reposted")
				return True, None
			try:
				ch = message.channel.parent if isinstance(message.channel, discord.Thread) else message.channel
				webhooks = await ch.webhooks()
				webhook = discord.utils.get(webhooks, name="IG Cleaner")
				if not webhook:
					webhook = await ch.create_webhook(name="IG Cleaner")
				kwargs = {"username": utils.webhook_name(message.author.display_name), "avatar_url": message.author.display_avatar.url}
				if isinstance(message.channel, discord.Thread):
					kwargs["thread"] = message.channel

				def buttons_view():
					view = discord.ui.View(timeout=None)
					# Instagram and X links get a button to the post itself (2x speed, replies, the rest of a long carousel)
					buttons = [(u, utils.open_button(u)) for u in unique if u in chosen]
					for url, (site, link) in [(u, b) for u, b in buttons if b][:4]:
						label = f"All {gallery[2]}" if url == gallery_url and gallery[2] > len(gallery[0]) else None
						view.add_item(discord.ui.Button(label=label, emoji=button_emoji(site), url=link))
						# the photos of a gallery are attached already, so it doesn't get a Download button
						download = None if url == gallery_url else utils.download_link(url, media.get(url), FXEMBED_HOST, INSTAFIX_API, chosen.get(url))
						if download:
							view.add_item(discord.ui.Button(emoji=button_emoji("download"), url=download))
					for buttons in song_buttons.values():
						for platform, link in buttons:
							view.add_item(discord.ui.Button(emoji=button_emoji(platform), url=link))
					view.add_item(discord.ui.Button(
						emoji="🗑️",
						style=discord.ButtonStyle.danger,
						custom_id=f"dellink:{message.author.id}",
					))
					return view

				async def send():
					files = [await a.to_file(spoiler=a.is_spoiler()) for a in message.attachments]
					if gallery:
						files += [discord.File(io.BytesIO(f.fp.getvalue()), filename=f.filename, spoiler=f.spoiler) for f in gallery[0]]
						kwargs["embed"] = gallery[1]
					if fallback:
						files.append(discord.File(io.BytesIO(fallback.fp.getvalue()), filename=fallback.filename, spoiler=fallback.spoiler))
					return await webhook.send(transformed, view=buttons_view(), files=files, wait=True, **kwargs)

				try:
					sent = await send()
				except discord.HTTPException as e:
					if not app_emojis or "emoji" not in str(e).lower():
						raise
					# Discord refused the logo emojis: use the plain ones from now on
					print(f"Logo emojis rejected, switching to plain ones: {e}", flush=True)
					app_emojis.clear()
					sent = await send()
			except discord.HTTPException as e:
				print(f"Link repost failed in #{message.channel}: {e}", flush=True)
				steps[next(iter(steps))].append(f"✗ reposting failed: {e}")
				return True, None
			# Send first, delete second: if the delete fails, drop the repost instead of losing the message.
			try:
				await message.delete()
			except discord.HTTPException as e:
				if not isinstance(e, discord.NotFound):
					print(f"Couldn't delete original in #{message.channel}: {e}", flush=True)
				await sent.delete()
				return True, None
			seen_links.remember(message.guild.id, keys, sent.id, sent.jump_url, message.author.display_name)
			repost_owners.add(sent.id, message.author.id)
			return True, sent

		@client.event
		async def on_message(message):
			if message.author==client.user:
				return

			username = str(message.author.name)
			user_message = str(message.content) if message.content else "<no message>"
			channel = str(message.channel)

			if user_message.startswith('\\'):
				return

			if not message.webhook_id and not message.author.bot and message.guild:
				if await reaction_from_message(message):
					return
				await fix_links(message, user_message)

			if user_message[0] == '.':
				rest = user_message[1:].strip()
				cmd = rest.split()[0].lower() if rest else ""
				if cmd in ("hello", "hey", "hi", "yo", "sup", "wassup", "hola", "inspire", "motivate") or "depressed" in rest.lower():
					print(f"{username} said: '{user_message}' in {channel}")
					user_message = user_message[1:]
					await send_message(message, user_message, False)

		@client.event
		async def on_raw_message_delete(payload):
			seen_links.forget(payload.message_id)  # a deleted repost no longer counts as posted before
			repost_owners.forget(payload.message_id)

		@client.event
		async def on_interaction(interaction: discord.Interaction):
			if not interaction.data or "custom_id" not in interaction.data:
				return
			cid = interaction.data["custom_id"]
			if not cid.startswith("dellink:"):
				return
			author_id = int(cid.split(":", 1)[1])
			if interaction.user.id != author_id and not interaction.user.guild_permissions.manage_messages:
				await interaction.response.send_message("Only the poster or a mod can do that.", ephemeral=True)
				return
			await interaction.response.defer()
			await interaction.message.delete()

		load_dotenv()
		GUILD_ID = os.environ.get("GUILD_ID")
		# InstaFix instance with the /api/p/<id> endpoint, used for multi-photo Instagram galleries
		INSTAFIX_API = "https://ig.reaper113.xyz"
		# Our FxEmbed instance: it translates non-English posts (link + /en) through the translator on the same server
		FXEMBED_HOST = "x.reaper113.xyz"
		FXEMBED_API = "https://xapi.reaper113.xyz"  # its JSON API: a post's photos and videos, for spoilered X links
		APPLE_COUNTRY = "IN"  # Apple Music store the "Apple Music" button links into
		game_log_channel = None
		url_rules = []
		fixed_cache = {}
		fix_details = {}  # message id (original and repost) -> per-link attempt lines
		seen_links = utils.SeenLinks("links.db")
		repost_owners = utils.RepostOwners("links.db")
		app_emojis = {}  # name -> the bot's application emoji (button logos from emojis/)
		http = None
		reactions = {}
		dead_reactions = {}  # name -> what the last check got from its link (shown in /listai)

		@client.event
		async def on_presence_update(before, after):
			nonlocal game_log_channel
			if not game_log_channel:
				return
			if before.activities == after.activities:
				return
			before_games = {a.name for a in before.activities if isinstance(a, discord.Game)}
			after_games = {a.name for a in after.activities if isinstance(a, discord.Game)}
			new_games = after_games - before_games
			for game in new_games:
				await game_log_channel.send(f"Yooo {after.mention} started playing **{game}**!")

		@tree.command(name="setgamechannel", description="Set channel for game activity logs")
		@discord.app_commands.default_permissions(manage_guild=True)
		async def set_game_channel(interaction: discord.Interaction, channel: discord.TextChannel):
			nonlocal game_log_channel
			if not channel.permissions_for(interaction.guild.me).send_messages:
				await interaction.response.send_message("I can't send messages there!", ephemeral=True)
				return
			data = {}
			try:
				with open("config.json") as f:
					data = json.load(f)  # keep url_rules, reactions and anything else saved there
			except (FileNotFoundError, json.JSONDecodeError):
				pass
			data["game_log_channel_id"] = channel.id
			with open("config.json", "w") as f:
				json.dump(data, f)
			game_log_channel = channel
			await interaction.response.send_message(f"Game logs will go to {channel.mention}", ephemeral=True)

		@tree.context_menu(name="Embed details")
		async def embed_details(interaction: discord.Interaction, message: discord.Message):
			"""Right-click a message → Apps → Embed details: what the link fixer did, only for you."""
			steps = fix_details.get(message.id)
			if not steps:
				await interaction.response.send_message(
					"No link fixing happened on this message, or it was too long ago "
					f"(details are kept for the last {DETAILS_KEPT} fixes since the bot started).",
					ephemeral=True,
				)
				return
			lines = ["**Embed fixing**"]
			for url, url_steps in steps.items():
				lines.append(utils.short_link(url))
				lines += [f"- {step}" for step in url_steps] or ["- nothing tried"]
			await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

		def repost_owner(message: discord.Message) -> int | None:
			"""Who a repost of ours belongs to: recorded when it was sent, or (for reposts from before
			that) the poster id in its Delete button."""
			if not message.webhook_id:
				return None
			owner = repost_owners.owner(message.id)
			if owner is not None:
				return owner
			for row in message.components:
				for item in getattr(row, "children", []):
					cid = getattr(item, "custom_id", None) or ""
					if cid.startswith("dellink:"):
						return int(cid.split(":", 1)[1])
			return None

		class EditRepost(discord.ui.Modal, title="Edit repost"):
			def __init__(self, message: discord.Message):
				super().__init__()
				self.message = message
				# a repost with files or a gallery can lose its text; one without can't
				self.text = discord.ui.TextInput(
					label="Message", style=discord.TextStyle.paragraph, default=message.content[:2000] or None,
					max_length=2000, required=not (message.attachments or message.embeds),
				)
				self.add_item(self.text)

			async def on_submit(self, interaction: discord.Interaction):
				try:
					webhook = await client.fetch_webhook(self.message.webhook_id)
					thread = self.message.channel if isinstance(self.message.channel, discord.Thread) else discord.utils.MISSING
					await webhook.edit_message(self.message.id, content=self.text.value, thread=thread)
				except discord.HTTPException as e:
					await interaction.response.send_message(f"Couldn't edit it: {e.text or e.status}", ephemeral=True)
					return
				await interaction.response.send_message("✏️ Edited.", ephemeral=True)

		@tree.context_menu(name="Edit repost")
		async def edit_repost(interaction: discord.Interaction, message: discord.Message):
			"""Right-click a repost → Apps → Edit repost: change its text (only whoever it's posted as)."""
			owner = repost_owner(message)
			if owner is None:
				await interaction.response.send_message("That isn't one of my reposts.", ephemeral=True)
				return
			if interaction.user.id != owner:
				await interaction.response.send_message("Only the person it was posted as can edit it.", ephemeral=True)
				return
			await interaction.response.send_modal(EditRepost(message))

		urlrule = discord.app_commands.Group(name="urlrule", description="Manage URL replacement rules")

		async def autocomplete_domains(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
			return [
				discord.app_commands.Choice(name=r["domain"], value=r["domain"])
				for r in url_rules if current.lower() in r["domain"].lower()
			][:25]

		@urlrule.command(name="add", description="Add a URL replacement rule")
		@discord.app_commands.describe(replacement="Fixer host, or several separated by commas to try in order")
		@discord.app_commands.default_permissions(manage_guild=True)
		async def urlrule_add(interaction: discord.Interaction, domain: str, replacement: str, drop: str = None):
			nonlocal url_rules
			for rule in url_rules:
				if rule["domain"] == domain:
					await interaction.response.send_message(f"Rule for `{domain}` already exists", ephemeral=True)
					return
			hosts = [h.strip() for h in replacement.split(",") if h.strip()]
			if not hosts:
				await interaction.response.send_message("Give at least one replacement host", ephemeral=True)
				return
			rule = {"domain": domain, "replacement": hosts if len(hosts) > 1 else hosts[0]}
			if drop:
				rule["drop"] = [p.strip() for p in drop.split(",") if p.strip()]
			url_rules.append(rule)
			data = {}
			try:
				with open("config.json") as f:
					data = json.load(f)
			except (FileNotFoundError, json.JSONDecodeError):
				pass
			data["url_rules"] = url_rules.copy()
			with open("config.json", "w") as f:
				json.dump(data, f)
			await interaction.response.send_message(f"✅ Rule added: `{domain}` → " + " → ".join(f"`{h}`" for h in hosts), ephemeral=True)

		@urlrule.command(name="remove", description="Remove a URL replacement rule")
		@discord.app_commands.autocomplete(domain=autocomplete_domains)
		@discord.app_commands.default_permissions(manage_guild=True)
		async def urlrule_remove(interaction: discord.Interaction, domain: str):
			nonlocal url_rules
			for i, rule in enumerate(url_rules):
				if rule["domain"] == domain:
					url_rules.pop(i)
					data = {}
					try:
						with open("config.json") as f:
							data = json.load(f)
					except (FileNotFoundError, json.JSONDecodeError):
						pass
					data["url_rules"] = url_rules.copy()
					with open("config.json", "w") as f:
						json.dump(data, f)
					await interaction.response.send_message(f"✅ Rule removed: `{domain}`", ephemeral=True)
					return
			await interaction.response.send_message(f"No rule found for `{domain}`", ephemeral=True)

		@urlrule.command(name="list", description="List all URL replacement rules")
		async def urlrule_list(interaction: discord.Interaction):
			nonlocal url_rules
			if not url_rules:
				await interaction.response.send_message("No rules configured.", ephemeral=True)
				return
			lines = [
				f"{i+1}. `{r['domain']}` → " + " → ".join(f"`{h}`" for h in utils.rule_hosts(r)) + (f" (drop: {', '.join(r['drop'])})" if r.get("drop") else "")
				for i, r in enumerate(url_rules)
			]
			await interaction.response.send_message("\n".join(lines), ephemeral=True			)

		tree.add_command(urlrule)

		# --- saved reactions: /addai, /ai, /removeai, /listai and ;name in a message ---

		def save_reactions():
			data = {}
			try:
				with open("config.json") as f:
					data = json.load(f)  # keep url_rules and anything else saved there
			except (FileNotFoundError, json.JSONDecodeError):
				pass
			data["reactions"] = dict(reactions)
			with open("config.json", "w") as f:
				json.dump(data, f)

		def reaction_file(name: str) -> str | None:
			"""Path of a reaction saved as an uploaded file, or None for a link."""
			value = reactions.get(name, "")
			return os.path.join(REACTIONS_DIR, value[5:]) if value.startswith("file:") else None

		def drop_reaction_file(name: str):
			path = reaction_file(name)
			if path and os.path.exists(path):
				os.remove(path)

		async def link_problem(url: str) -> str | None:
			"""What's wrong with a saved link, or None if it works or we can't tell (our network)."""
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(url, timeout=aiohttp.ClientTimeout(total=8), headers={"Range": "bytes=0-0"}) as resp:
					return f"HTTP {resp.status}" if resp.status in DEAD_STATUSES else None
			except Exception as e:
				print(f"Reaction link check failed for {url}: {e.__class__.__name__}", flush=True)
				return None

		async def download_upload(url: str) -> tuple[bytes, str] | None:
			"""(data, extension) of a Discord attachment link, or None if it expired or is too big."""
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
					if resp.status != 200 or (resp.content_length or 0) > REACTION_MAX_BYTES:
						return None
					data = await resp.read()
			except Exception as e:
				print(f"Reaction download failed for {url}: {e.__class__.__name__}", flush=True)
				return None
			if len(data) > REACTION_MAX_BYTES:
				return None
			return data, utils.reaction_extension(url)

		def store_reaction_file(name: str, data: bytes, ext: str):
			drop_reaction_file(name)
			os.makedirs(REACTIONS_DIR, exist_ok=True)
			with open(os.path.join(REACTIONS_DIR, name + ext), "wb") as f:
				f.write(data)
			reactions[name] = f"file:{name}{ext}"
			dead_reactions.pop(name, None)

		async def webhook_for(channel) -> tuple[discord.Webhook, dict]:
			"""The bot's "IG Cleaner" webhook for channel, plus the send() kwargs to post into it.
			Webhooks belong to the parent channel; a thread is reached with thread=."""
			parent = channel.parent if isinstance(channel, discord.Thread) else channel
			webhook = discord.utils.get(await parent.webhooks(), name="IG Cleaner")
			if not webhook:
				webhook = await parent.create_webhook(name="IG Cleaner")
			return webhook, ({"thread": channel} if isinstance(channel, discord.Thread) else {})

		async def send_reaction(channel, member, name: str, target: str | None = None, reply_to=None) -> str | None:
			"""Post a saved reaction as member. None when sent, else why not (to show the user)."""
			value = reactions.get(name)
			if value is None:
				return f"No reaction named `{name}`."
			path = reaction_file(name)
			if path and not os.path.exists(path):
				return f"`{name}`'s file is missing. Add it again with /addai."
			if not path:
				problem = await link_problem(value)
				if problem:
					dead_reactions[name] = problem
					return f"`{name}`'s link is dead ({problem}). Add it again with /addai (uploading the file keeps it from dying)."
			lines = []
			if isinstance(reply_to, discord.Message):
				lines.append(f"-# ↪ replying to [{reply_to.author.display_name}]({reply_to.jump_url})")
			lines.append(" ".join(x for x in (target, None if path else value) if x))
			webhook, kwargs = await webhook_for(channel)
			sent = await webhook.send(
				"\n".join(line for line in lines if line) or discord.utils.MISSING,
				file=discord.File(path) if path else discord.utils.MISSING,
				username=utils.webhook_name(member.display_name),
				avatar_url=member.display_avatar.url,
				wait=True,
				**kwargs,
			)
			repost_owners.add(sent.id, member.id)
			return None

		async def reaction_from_message(message) -> bool:
			"""A message that's just ;name (optionally with one @mention) posts that reaction as its
			author and is deleted. True if it was one."""
			m = REACTION_TRIGGER_RE.fullmatch(message.content.strip())
			if not m or message.attachments:
				return False
			name = {k.lower(): k for k in reactions}.get(m.group(1).lower())
			if not name:
				return False
			ref = message.reference.resolved if message.reference else None
			problem = await send_reaction(message.channel, message.author, name, m.group(2), ref)
			if problem:
				try:
					await message.reply(problem, delete_after=15, mention_author=False)
				except discord.HTTPException:
					pass
				return True
			try:
				await message.delete()
			except discord.HTTPException:
				pass
			return True

		@tasks.loop(hours=24)
		async def check_reactions():
			"""Daily: note which saved links stopped working (shown in /listai)."""
			for name, value in list(reactions.items()):
				if value.startswith("file:"):
					continue
				problem = await link_problem(value)
				if problem:
					dead_reactions[name] = problem
				else:
					dead_reactions.pop(name, None)
			if dead_reactions:
				print(f"Dead reaction links: {', '.join(sorted(dead_reactions))}", flush=True)

		async def _autocomplete_reactions(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
			return [
				discord.app_commands.Choice(name=name, value=name)
				for name in sorted(reactions) if current.lower() in name.lower()
			][:25]

		@tree.command(name="addai", description="Save a reaction: a link, or upload the GIF/image/video itself")
		@discord.app_commands.describe(
			name="What to call it (letters, digits, _ and -); type ;name in a message to post it",
			link="A link to the GIF/image (Tenor, Giphy, ...)",
			file="Or upload it: kept by the bot, so it never stops working",
		)
		@discord.app_commands.default_permissions(manage_guild=True)
		async def add_react(interaction: discord.Interaction, name: str, link: str | None = None, file: discord.Attachment | None = None):
			if not REACTION_NAME_RE.fullmatch(name):
				await interaction.response.send_message("Names can only have letters, digits, `_` and `-` (up to 32).", ephemeral=True)
				return
			if not link and not file:
				await interaction.response.send_message("Give a link or upload a file.", ephemeral=True)
				return
			await interaction.response.defer(ephemeral=True)
			if file:
				if file.size > REACTION_MAX_BYTES:
					await interaction.followup.send(f"That file is over {REACTION_MAX_BYTES // 2**20} MB.", ephemeral=True)
					return
				store_reaction_file(name, await file.read(), utils.reaction_extension(file.filename))
				kind = "uploaded file"
			elif not link.startswith(("http://", "https://")):
				await interaction.followup.send("Links must start with http:// or https://", ephemeral=True)
				return
			elif utils.is_discord_upload(link):
				# Discord's file links expire after about a day, so keep the file itself
				got = await download_upload(link)
				if not got:
					await interaction.followup.send("That Discord file link has expired (or is over 10 MB). Upload the file instead.", ephemeral=True)
					return
				store_reaction_file(name, *got)
				kind = "file (Discord links expire, so the bot keeps a copy)"
			else:
				problem = await link_problem(link)
				if problem:
					await interaction.followup.send(f"That link doesn't work ({problem}).", ephemeral=True)
					return
				drop_reaction_file(name)
				reactions[name] = link
				dead_reactions.pop(name, None)
				kind = "link"
			save_reactions()
			await interaction.followup.send(f"✅ Saved `{name}` ({kind}). Post it with `/ai {name}` or by typing `;{name}`.", ephemeral=True)

		@tree.command(name="removeai", description="Delete a saved reaction")
		@discord.app_commands.autocomplete(name=_autocomplete_reactions)
		@discord.app_commands.default_permissions(manage_guild=True)
		async def remove_react(interaction: discord.Interaction, name: str):
			if name not in reactions:
				await interaction.response.send_message(f"No reaction named `{name}`.", ephemeral=True)
				return
			drop_reaction_file(name)
			del reactions[name]
			dead_reactions.pop(name, None)
			save_reactions()
			await interaction.response.send_message(f"🗑️ Removed `{name}`.", ephemeral=True)

		@tree.command(name="listai", description="List the saved reactions")
		async def list_reacts(interaction: discord.Interaction):
			if not reactions:
				await interaction.response.send_message("No reactions saved yet. Add one with /addai.", ephemeral=True)
				return
			# dead ones first, so they get fixed; one embed (a message's embeds share a 6000-character cap)
			names = sorted(reactions, key=lambda n: (n not in dead_reactions, n.lower()))
			items = [
				f"`{name}`" + (" 📎" if reactions[name].startswith("file:") else "") + (f" ⚠️ dead ({dead_reactions[name]})" if name in dead_reactions else "")
				for name in names
			]
			embed = discord.Embed(title=f"Saved reactions ({len(reactions)})", description=utils.fit_items(items, 4000), color=0x5865F2)
			embed.set_footer(text="Post one: /ai <name> or type ;name · 📎 = uploaded file, kept by the bot")
			await interaction.response.send_message(embed=embed, ephemeral=True)

		@tree.command(name="ai", description="Send a saved reaction")
		@discord.app_commands.autocomplete(name=_autocomplete_reactions)
		async def react(interaction: discord.Interaction, name: str, target: discord.Member = None):
			await interaction.response.defer(ephemeral=True)
			problem = await send_reaction(interaction.channel, interaction.user, name, target.mention if target else None)
			await interaction.followup.send(problem or "✅", ephemeral=True)

		client.run(os.environ['BOT_TOKEN'])