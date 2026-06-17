import discord, os, utils
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
		client = discord.Client(intents=intents)

		@client.event
		async def on_ready():
			await client.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="your feelings"))
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
		client.run(os.environ['BOT_TOKEN'])