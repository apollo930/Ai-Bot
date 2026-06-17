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
			nonlocal game_log_channel
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
							print(f"Game log channel set to #{ch.name}")
						else:
							print(f"Warning: config.json channel {ch_id} not found or not messageable")
					else:
						print("No game_log_channel_id in config.json")
			except FileNotFoundError:
				print("No config.json found — use /setgamechannel to configure game tracking")
			await tree.sync(guild=discord.Object(id=1053695451234316358))
			print(f"{client.user} is ready!")

		@client.event
		async def on_message(message):
			if message.author==client.user:
				return

			username = str(message.author.name)
			user_message = str(message.content) if message.content else "<no message>"
			channel = str(message.channel)

			print(f"{username} said: '{user_message}' in {channel}")

			transformed = utils.transform_instagram_links(user_message)
			if transformed:
				await message.reply(transformed)

			if user_message[0] == '.':
				user_message = user_message[1:]
				await send_message(message, user_message, False)
				
		load_dotenv()
		game_log_channel = None

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
			with open("config.json", "w") as f:
				json.dump({"game_log_channel_id": channel.id}, f)
			game_log_channel = channel
			await interaction.response.send_message(f"Game logs will go to {channel.mention}", ephemeral=True)

		client.run(os.environ['BOT_TOKEN'])