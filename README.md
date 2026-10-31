# Ai-Bot

A Discord bot for a personal server.

## Setup

1. `uv sync`
2. Copy `.env.example` to `.env` and fill in:
   - `BOT_TOKEN` — Discord bot token
   - `GUILD_ID` — (optional) server ID for instant slash commands
3. `uv run python3 main.py`

## Tests

`uv run --frozen --with pytest pytest -q` (GitHub runs the same on every push). `tests/test_utils.py`
covers the link logic; `tests/test_bot.py` drives the commands and handlers with stand-ins for
Discord objects, without connecting or touching the network.

## Commands

| Command | Description |
|---|---|
| `/setgamechannel` | Set channel for game activity announcements |
| `/urlrule add <domain> <replacement> [drop]` | Rewrite links to an embed fixer. `replacement` can list several hosts, comma-separated (`ig.reaper113.xyz,kkinstagram.com`): each is checked in order and the first one that gives Discord an image/video wins. If none does, the message is left alone. |
| `/urlrule remove` / `/urlrule list` | Manage the rules |
| `/addai <name> [link] [file]` | Save a reaction (Manage Server). Upload the GIF/image/video itself and the bot keeps the file (up to 10 MB) in `data/reactions/`, so it never stops working; Discord file links, which expire after about a day, are kept as files the same way. Other links are checked before saving. |
| `/ai <name> [target]` | Post a saved reaction as you (works in threads too). A link that has died is refused with a note to add it again. |
| `;name` in a message | Same as `/ai name`: a message that's just `;name` (optionally `;name @someone`, or as a reply) is replaced by the reaction. |
| `/listai` | List the saved reactions (only to you). 📎 = kept as a file; ⚠️ = its link died (links are checked daily). |
| `/removeai <name>` | Delete a saved reaction (Manage Server). |
| `/reactions [user] [channel]` | Reactions received on your messages (or another member's), with an emoji breakdown and a link to their most-reacted message if you can access it. Optionally limit results to one channel or thread. |
| `/reactionboard [emoji] [channel]` | Server leaderboard by reactions received. Optionally filter by emoji, channel/thread, or both. Emoji filters accept Unicode emoji, custom emoji such as `<:name:123456789>`, or custom emoji IDs. |
| `/reactionscan <limit> [channel] [all_channels]` | Administrators only: scan the last N messages, or `inf` for all history. Defaults to the current channel/thread. Set `all_channels:true` for the server; a numeric limit then applies separately to each channel/thread. |
| `.hello` / `.hi` | Greeting |
| `.inspire` / `.motivate` | Random quote |

### Reaction totals

- Counts reflect reactions currently on messages, across this server. Each person's different emojis count separately; a normal and super reaction with the same emoji from the same person count once. Bots and self-reactions are excluded, including the bot's own ⏳ and ⚠️ reactions.
- Leave `channel` empty for server-wide results. Selecting a channel counts messages directly in that channel; select an individual thread to count its messages separately. For example, `/reactions user:@Alex channel:#general` or `/reactionboard emoji:😂 channel:#memes`. You must be able to view the selected channel/thread and read its history.
- Reactions on the bot's link reposts and `/ai` posts credit the original person. Other bots and unrelated webhook posts do not receive credit.
- Adds, removals, cleared reactions, and deleted messages update the counts live, including messages outside the bot's memory cache. Repeated events do not increase totals twice. Rankings show up to 20 people and never ping them.
- History scans run **only when an administrator requests one**; startup, redeploys, and reconnects never trigger a history scan. Live tracking starts immediately using saved totals. Reactions changed while offline can remain stale until their messages are rescanned (or receive another live reaction event).
- `/reactionscan limit:1000` scans the newest 1,000 messages in the current channel/thread. Add `channel:#memes` to choose another channel. `/reactionscan limit:inf` scans the current channel's entire history. Add `all_channels:true` to either command to scan all accessible channels and active/archived threads; numeric limits apply **per channel/thread**, not as a server-wide total. Selecting a single channel does not include its threads. `channel` and `all_channels:true` cannot be combined.
- Scans run in the background; `/reactions` and `/reactionboard` show progress and the latest scan's scope and completion time. Only one scan can run at a time. Repeat scans refresh counts without duplicating them; limited scans retain older saved totals outside their inspected range. Full-history scans can take a while on a server with a long history. Cancelling the bot/redeploying interrupts a scan; it does not resume automatically.
- The bot needs **View Channel** and **Read Message History** for channels being counted. Server-wide scans include public threads and joined private threads; with **Manage Threads**, the bot can also discover other private archived threads. Missing access or API errors appear in the command footer and logs. Previously recorded counts in unavailable channels are retained until they can be reconciled, so partial totals can be stale as well as incomplete. Discord cannot recover reactions on messages already deleted before the first scan.
- Counts and scan status live in the existing, gitignored `links.db`. Tables are created automatically without changing existing repost/link records. On the Raspberry Pi, preserve this database across auto-pulls and redeploys; back it up alongside `config.json` and `data/`. No new dependencies, credentials, or environment variables are needed.

### Link reposts

- Discord doesn't preview links inside spoiler bars, so a spoilered Instagram or X link gets the post's photos and videos attached as spoilers instead (click to reveal), with the caption spoilered too. X videos use the best quality that fits the upload limit (via our FxEmbed's API, `FXEMBED_API`).
- Instagram carousels are reposted with their photos and videos attached in order (up to 10, as a Discord gallery; videos play inline, anything over the server's upload limit is counted in the footer instead) using the media API of the InstaFix instance at `ig.reaper113.xyz` (`INSTAFIX_API` in `bot.py`).
- Buttons on reposts are emoji-only: brand logos from `emojis/` (uploaded on startup as the bot's own application emojis; plain emojis until then), and a red 🗑️ Delete. Instagram and X links get a logo button that opens the post. Posts with a video or photo also get a download button, which opens the original file. Galleries don't get one, because their photos are already attached.
- Non-English X posts go to `x.reaper113.xyz` with `/en`, which translates them. Instagram captions are translated by `ig.reaper113.xyz` itself.
- When a post was already shared in the server, the repost says "🔁 posted before by …", linking to that first repost. The link is masked (`[text](<url>)`) so Discord doesn't preview the old message. This history is kept in `links.db` next to `config.json`. Deleting the first repost removes it from the history.
- Single-song Spotify, Apple Music and YouTube Music links are reposted with logo buttons for the other platforms. Apple Music links straight to the song, found with Apple's free search API. Spotify and YouTube Music open a search for "artist title". 🔗 opens the song.link page. Albums and playlists are left alone. `APPLE_COUNTRY` in `bot.py` picks the Apple Music store.
- When no fixer can embed a post, the bot tries to download its video with yt-dlp (up to the server's upload limit, 60 s) and attaches it instead, keeping the link itself from embedding. One per message. Posts Instagram restricts to logged-in users can't be fetched this way either.
- While a link is being fixed, the original message gets ⏳; if no fixer works it gets ⚠️. Right-click → Apps → "Embed details" shows what each fixer answered (only to you).
- Reposts can't be edited the normal way (they're the webhook's messages), so right-click → Apps → "Edit repost" opens the text to change. Only the person it was posted as can use it. Owners of link reposts and reactions are kept in `links.db`; older reposts are matched through their Delete button.
