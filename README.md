# Ai-Bot

A Discord bot for a personal server.

## Setup

1. `uv sync`
2. Copy `.env.example` to `.env` and fill in:
   - `BOT_TOKEN` — Discord bot token
   - `GUILD_ID` — (optional) server ID for instant slash commands
3. `uv run python3 main.py`

## Commands

| Command | Description |
|---|---|
| `/setgamechannel` | Set channel for game activity announcements |
| `/urlrule add <domain> <replacement> [drop]` | Rewrite links to an embed fixer. `replacement` can list several hosts, comma-separated (`ig.reaper113.xyz,kkinstagram.com`): each is checked in order and the first one that gives Discord an image/video wins. If none does, the message is left alone. |
| `/urlrule remove` / `/urlrule list` | Manage the rules |
| `.hello` / `.hi` | Greeting |
| `.inspire` / `.motivate` | Random quote |

### Link reposts

- Multi-photo Instagram posts are reposted with the photos attached (up to 10, as a Discord gallery) using the media API of the InstaFix instance at `ig.reaper113.xyz` (`INSTAFIX_API` in `bot.py`).
- Buttons on reposts are emoji-only: brand logos from `emojis/` (uploaded on startup as the bot's own application emojis; plain emojis until then), and a red 🗑️ Delete. Instagram and X links get a logo button that opens the post. Posts with a video or photo also get a download button, which opens the original file. Galleries don't get one, because their photos are already attached.
- Non-English X posts go to `x.reaper113.xyz` with `/en`, which translates them. Instagram captions are translated by `ig.reaper113.xyz` itself.
- When a post was already shared in the server, the repost says "🔁 posted before by …" and links to that first repost. This history is kept in `links.db` next to `config.json`. Deleting the first repost removes it from the history.
- Single-song Spotify, Apple Music and YouTube Music links are reposted with logo buttons for the other platforms. Apple Music links straight to the song, found with Apple's free search API. Spotify and YouTube Music open a search for "artist title". 🔗 opens the song.link page. Albums and playlists are left alone. `APPLE_COUNTRY` in `bot.py` picks the Apple Music store.
- While a link is being fixed, the original message gets ⏳; if no fixer works it gets ⚠️. Right-click → Apps → "Embed details" shows what each fixer answered (only to you).
