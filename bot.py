import discord, os, utils, json
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

		@client.event
		async def on_message(message):
			if message.author==client.user:
				return

			username = str(message.author.name)
			user_message = str(message.content) if message.content else "<no message>"
			channel = str(message.channel)

			if user_message.startswith('\\'):
				return

			transformed = utils.transform_urls(user_message, url_rules)
			if transformed:
				try:
					await message.delete()
					webhooks = await message.channel.webhooks()
					webhook = discord.utils.get(webhooks, name="IG Cleaner")
					if not webhook:
						webhook = await message.channel.create_webhook(name="IG Cleaner")
					await webhook.send(transformed, username=message.author.display_name, avatar_url=message.author.display_avatar.url)
				except:
					pass

			if user_message[0] == '.':
				rest = user_message[1:].strip()
				cmd = rest.split()[0].lower() if rest else ""
				if cmd in ("hello", "hey", "hi", "yo", "sup", "wassup", "hola", "inspire", "motivate") or "depressed" in rest.lower():
					print(f"{username} said: '{user_message}' in {channel}")
					user_message = user_message[1:]
					await send_message(message, user_message, False)

				
		load_dotenv()
		GUILD_ID = os.environ.get("GUILD_ID")
		game_log_channel = None
		url_rules = []
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
		@discord.app_commands.default_permissions(manage_guild=True)
		async def urlrule_add(interaction: discord.Interaction, domain: str, replacement: str):
			nonlocal url_rules
			for rule in url_rules:
				if rule["domain"] == domain:
					await interaction.response.send_message(f"Rule for `{domain}` already exists", ephemeral=True)
					return
			url_rules.append({"domain": domain, "replacement": replacement})
			data = {}
			try:
				with open("config.json") as f:
					data = json.load(f)
			except (FileNotFoundError, json.JSONDecodeError):
				pass
			data["url_rules"] = url_rules.copy()
			with open("config.json", "w") as f:
				json.dump(data, f)
			await interaction.response.send_message(f"✅ Rule added: `{domain}` → `{replacement}`", ephemeral=True)

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
			lines = [f"{i+1}. `{r['domain']}` → `{r['replacement']}`" for i, r in enumerate(url_rules)]
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