import discord, os, utils, json, asyncio, time, aiohttp, io, glob
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
					html = (await resp.content.read(256 * 1024)).decode("utf-8", errors="replace")
					if page is not None:
						page["lang"] = utils.page_lang(html)
						page["media"] = utils.page_media(html)
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
			Each attempt goes into steps, for "Apps → Embed details"."""
			hit = fixed_cache.get(url)
			if hit and time.time() - hit[0] < 600:
				steps.append(f"✓ `{utils.host_of(hit[1])}`: worked for this link in the last 10 min, reused")
				return hit[1], hit[2]
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
				if problem is None:
					steps.append(f"✓ `{host}`: used{note}")
					media = pages[candidate].get("media")
					if len(fixed_cache) > 500:
						fixed_cache.clear()
					fixed_cache[url] = (time.time(), candidate, media)
					return candidate, media
				steps.append(f"✗ `{host}`: {problem}")
				print(f"Fixer {host} has no embed for {url}: {problem}", flush=True)
			return None, None

		async def instagram_gallery(url: str, room: int, size_limit: int):
			"""For an Instagram carousel: (files, embed, total items), so the repost carries the
			real photos and videos, in order, as a Discord gallery instead of one grid image
			(videos play inline). Items over the upload limit are left out and counted in the
			footer. None for single photos, reels, or when INSTAFIX_API isn't set."""
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
			if len(media) < 2:
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
						return discord.File(io.BytesIO(data), filename=f"{code}_{i + 1}{ext}")
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
					html = (await resp.content.read(256 * 1024)).decode("utf-8", errors="replace") if resp.status == 200 else ""
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

			# One multi-photo Instagram post per message becomes a real gallery (attachments cap at 10).
			gallery, gallery_url = None, None
			for url in unique:
				gallery = await instagram_gallery(url, 10 - len(message.attachments), limit)
				if gallery:
					gallery_url = url
					steps[url].append(f"📷 attached {len(gallery[0])} of {gallery[2]} photos/videos as a gallery (via `{utils.host_of(INSTAFIX_API)}`)")
					# the photos are attached, so the link itself shouldn't embed a grid too
					chosen[url] = f"<{utils.instagram_url(url)}>"
					break
			if not chosen and not song_buttons:
				return False, None  # no fixer works right now: leave the original alone
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
						files += [discord.File(io.BytesIO(f.fp.getvalue()), filename=f.filename) for f in gallery[0]]
						kwargs["embed"] = gallery[1]
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
		APPLE_COUNTRY = "IN"  # Apple Music store the "Apple Music" button links into
		game_log_channel = None
		url_rules = []
		fixed_cache = {}
		fix_details = {}  # message id (original and repost) -> per-link attempt lines
		seen_links = utils.SeenLinks("links.db")
		app_emojis = {}  # name -> the bot's application emoji (button logos from emojis/)
		http = None
		reactions = {}

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

		@tree.command(name="addai", description="Save a reaction link")
		@discord.app_commands.default_permissions(manage_guild=True)
		async def add_react(interaction: discord.Interaction, name: str, link: str):
			nonlocal reactions
			if not link.startswith(("http://", "https://")):
				await interaction.response.send_message("Link must start with http:// or https://", ephemeral=True)
				return
			reactions[name] = link
			data = {}
			try:
				with open("config.json") as f:
					data = json.load(f)
			except (FileNotFoundError, json.JSONDecodeError):
				pass
			data["reactions"] = dict(reactions)
			with open("config.json", "w") as f:
				json.dump(data, f)
			await interaction.response.send_message(f"✅ Reaction `{name}` added!", ephemeral=True)

		async def _autocomplete_reactions(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
			return [
				discord.app_commands.Choice(name=name, value=name)
				for name in reactions if current.lower() in name.lower()
			][:25]

		@tree.command(name="ai", description="Send a saved reaction")
		@discord.app_commands.autocomplete(name=_autocomplete_reactions)
		async def react(interaction: discord.Interaction, name: str, target: discord.Member = None):
			nonlocal reactions
			if name not in reactions:
				await interaction.response.send_message(f"No reaction named `{name}`", ephemeral=True)
				return
			link = reactions[name]
			if target:
				link = f"{target.mention} {link}"
			await interaction.response.send_message("✅", ephemeral=True)
			webhooks = await interaction.channel.webhooks()
			webhook = discord.utils.get(webhooks, name="IG Cleaner")
			if not webhook:
				webhook = await interaction.channel.create_webhook(name="IG Cleaner")
			await webhook.send(
				link,
				username=interaction.user.display_name,
				avatar_url=interaction.user.display_avatar.url,
			)

		client.run(os.environ['BOT_TOKEN'])