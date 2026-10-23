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

Multi-photo Instagram posts are reposted with the photos attached (up to 10, as a Discord gallery) using the media API of the InstaFix instance at `ig.reaper113.xyz` (`INSTAFIX_API` in `bot.py`), and Instagram links get an "Open on Instagram" button.
| `.hello` / `.hi` | Greeting |
| `.inspire` / `.motivate` | Random quote |
