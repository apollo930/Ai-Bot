import discord, os, utils, json, asyncio, time, aiohttp
from discord.ext import tasks
from dotenv import load_dotenv

async def send_message(message, user_message, is_private):
	try:
		response=utils.get_response(user_message)
		await message.author.send(response) if is_private else await message.channel.send(response)

	except Exception as e:
		print("Error: " + str(e))


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
			print(f"{client.user} is ready!", flush=True)

		async def embeds(url: str) -> bool:
			"""Would Discord get a media embed from this fixer URL?"""
			nonlocal http
			if http is None or http.closed:
				http = aiohttp.ClientSession(headers={"User-Agent": utils.PROBE_UA})
			try:
				async with http.get(url, timeout=aiohttp.ClientTimeout(total=6), max_redirects=5) as resp:
					if resp.status != 200:
						return False
					# kkinstagram-style fixers redirect Discordbot straight to the media file
					if resp.content_type.startswith(("image/", "video/")):
						return True
					if "html" not in resp.content_type:
						return False
					body = await resp.content.read(256 * 1024)
					return utils.has_embed_media(body.decode("utf-8", errors="replace"))
			except Exception as e:
				print(f"Probe failed for {url}: {e.__class__.__name__}", flush=True)
				return False

		async def resolve(url: str, rule: dict) -> str | None:
			"""First fixer URL for url that embeds, trying the rule's hosts in order."""
			hit = fixed_cache.get(url)
			if hit and time.time() - hit[0] < 600:
				return hit[1]
			for host in utils.rule_hosts(rule):
				candidate = utils.fix_url(url, rule, host)
				if await embeds(candidate):
					if len(fixed_cache) > 500:
						fixed_cache.clear()
					fixed_cache[url] = (time.time(), candidate)
					return candidate
				print(f"Fixer {host} has no embed for {url}", flush=True)
			return None

		async def fix_links(message, user_message):
			links = utils.find_links(user_message, url_rules)[:5]
			if not links or message.flags.suppress_embeds:
				return
			if sum(a.size for a in message.attachments) > message.guild.filesize_limit:
				return
			unique = {url: rule for _s, _e, url, rule in links}
			results = await asyncio.gather(*(resolve(u, r) for u, r in unique.items()))
			chosen = {u: fixed for u, fixed in zip(unique, results) if fixed}
			if not chosen:
				return  # no fixer works right now: leave the original alone
			transformed = utils.rewrite_links(user_message, links, chosen)
			ref = message.reference.resolved if message.reference else None
			if isinstance(ref, discord.Message):
				transformed = f"-# ↪ replying to [{ref.author.display_name}]({ref.jump_url})\n{transformed}"
			if len(transformed) > 2000:
				return
			try:
				ch = message.channel.parent if isinstance(message.channel, discord.Thread) else message.channel
				webhooks = await ch.webhooks()
				webhook = discord.utils.get(webhooks, name="IG Cleaner")
				if not webhook:
					webhook = await ch.create_webhook(name="IG Cleaner")
				kwargs = {"username": utils.webhook_name(message.author.display_name), "avatar_url": message.author.display_avatar.url}
				if isinstance(message.channel, discord.Thread):
					kwargs["thread"] = message.channel
				view = discord.ui.View(timeout=None)
				view.add_item(discord.ui.Button(
					label="Delete", emoji="🗑️",
					style=discord.ButtonStyle.secondary,
					custom_id=f"dellink:{message.author.id}",
				))
				files = [await a.to_file(spoiler=a.is_spoiler()) for a in message.attachments]
				sent = await webhook.send(transformed, view=view, files=files, wait=True, **kwargs)
			except discord.HTTPException as e:
				print(f"Link repost failed in #{message.channel}: {e}", flush=True)
				return
			# Send first, delete second: if the delete fails, drop the repost instead of losing the message.
			try:
				await message.delete()
			except discord.HTTPException as e:
				if not isinstance(e, discord.NotFound):
					print(f"Couldn't delete original in #{message.channel}: {e}", flush=True)
				await sent.delete()

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
		async def on_interaction(interaction: discord.Interaction):
			if not interaction.data or "custom_id" not in interaction.data:
				return
			cid = interaction.data["custom_id"]
			if not cid.startswith("dellink:"):
				return
			author_id = int(cid.split(":", 1)[1])
			if interaction.user.id != author_id and not interaction.user.guild_permissions.manage_messages:
				await interaction.response.send_message("Not your link!", ephemeral=True)
				return
			await interaction.response.defer()
			await interaction.message.delete()

		load_dotenv()
		GUILD_ID = os.environ.get("GUILD_ID")
		game_log_channel = None
		url_rules = []
		fixed_cache = {}
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
			data = {"game_log_channel_id": channel.id}
			try:
				with open("config.json") as f:
					data["url_rules"] = json.load(f).get("url_rules", [])
			except (FileNotFoundError, json.JSONDecodeError):
				pass
			with open("config.json", "w") as f:
				json.dump(data, f)
			game_log_channel = channel
			await interaction.response.send_message(f"Game logs will go to {channel.mention}", ephemeral=True)

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