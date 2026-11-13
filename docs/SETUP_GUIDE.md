# Setup guide (no coding needed)

This takes about 15 minutes. You'll install Python, install the tool, make a Roblox
API key, and see your first daily check-up. Everything stays on your computer.

## 1. Install Python (once)

1. Go to https://www.python.org/downloads/ and download Python 3.12 or newer.
2. **Windows:** in the installer, tick **"Add python.exe to PATH"** at the bottom, then click Install.
   **Mac:** run the installer normally.
3. Open a terminal: on Windows press Start, type **PowerShell**, open it. On Mac open **Terminal**.

## 2. Install the tool

Paste this line into the terminal and press Enter:

```
python -m pip install https://github.com/Boking234/roblox-analytics-agent/archive/refs/heads/main.zip
```

(On Mac, use `python3` instead of `python` everywhere in this guide.)

Check it worked:

```
roblox-agent --version
```

If that says "not recognized", use `python -m roblox_agent` in place of `roblox-agent` in every
command below (for example `python -m roblox_agent setup`).

## 3. Try it with pretend data first (optional, 1 minute)

```
roblox-agent demo
```

A page opens in your browser showing a check-up with made-up numbers, including a pretend crash
spike on phones. This is what the real one looks like. Nothing is sent anywhere.

## 4. Make a Roblox API key

1. Open https://create.roblox.com/dashboard/credentials and sign in.
2. Click **API Keys**, then **Create API Key**.
3. **Name:** anything, for example `analytics-tool`.
4. **Access Permissions:** under "Select API System" pick **universe-analytics** (search "analytics").
   Click **Add API System**, choose **your game**, and tick **universe.analytics:read**. Nothing else.
5. **Security / Accepted IP Addresses:** add `0.0.0.0/0` (any address). This is needed because
   home internet addresses change. The key is still useless without the key text itself.
6. **Expiration:** optional. If you set one, you'll need to make a new key when it runs out.
7. Click **Save & Generate Key**, then **Copy Key to Clipboard**. Roblox shows it only once.
   Don't paste it into chats, Discord, or GitHub.

## 5. Find your Universe ID

Open https://create.roblox.com/dashboard/creations, click your game. The link in the address bar
looks like `.../experiences/1234567890/overview`. The number is the Universe ID. (You can paste
the whole link in the next step; the tool picks out the number.)

## 6. Run it

```
roblox-agent
```

The first time, a 3-step setup starts by itself:

1. **Your game:** paste the dashboard link (or the Universe ID) and press Enter.
2. **Your key:** paste it and press Enter. **It stays hidden while you paste. That's normal.**
   The tool checks it with Roblox first and only saves it if it works.
   It then downloads your history (a year of players, retention and money, plus the last 27 days
   of performance), which takes a few minutes the first time.
3. **Your AI helper (optional):** pick the agent you use. The options are Claude Code, the Claude
   Desktop app, Codex, Cursor, VS Code, Gemini CLI and Windsurf. Or press Enter to skip.

It finishes by showing your first check-up:

```
Roblox game check-up, universe 123, data through 2026-11-12: Watch
1. ISSUE: Client crash rate (15 min) on Phone up 60% (0.031 vs usual 0.019, 2026-11-12)
2. GOOD: Daily revenue up 15% (6,200 vs usual 5,400, 2026-11-12)
```

After that, just run `roblox-agent` once a day. It fetches what's new and shows the top 3.
Roblox deletes performance data after 28 days, so run it at least weekly.

If the key isn't accepted, check that it has your game and `universe.analytics:read`, and that
the IP setting allows `0.0.0.0/0`. Then run `roblox-agent setup` again.

## 7. Use it with your AI agent

If you skipped step 3, run `roblox-agent connect` any time and pick your agent. Restart the
agent, then ask it: **"Run my Roblox daily check-up."**

- The agent receives only the short check-up text (about 300 to 400 tokens, less than 0.2% of
  reading a 1 MB file). It never sees your raw data unless you ask for a metric's history.
- Your API key is never put in the agent's settings and never sent to the agent.

## 8. More views

| Command | What you get |
| --- | --- |
| `roblox-agent view` | The check-up as a page, with every chart under "All metrics" |
| `roblox-agent demo` | The same with made-up numbers, including a pretend crash spike |
| `roblox-agent show` | A table of every metric |
| `roblox-agent export` | A CSV file for Excel or Google Sheets |

## 9. Check it against the Roblox dashboard

Open your game on the Creator Dashboard, go to **Analytics**, and set the date range to the
last 7 days. Then compare with `roblox-agent view` (open "All metrics"):

- **Daily active users, Visits, Daily revenue:** each day should match exactly, or within 1-2%.
- **Day 1 / Day 7 retention, Paying user conversion, crash rate:** the tool shows these as
  fractions, so `0.21` here means `21%` on the dashboard.
- **Days:** both use UTC days. If you're far from UTC, the dashboard's "today" may be a day ahead.
- **Latest day or two:** marked "estimate, may change". Roblox revises those; the next `pull`
  updates them automatically.
- **"Too few players to trust":** Roblox didn't have enough players that day to give a reliable
  number. If most days are marked like this, tell Claude: later checks will need more data or a
  partner game.

Tell Claude in the project thread what matched and what didn't (a screenshot of both is perfect).
**Never include the API key.**

## Removing the key

`roblox-agent forget-key` deletes it from your computer. To cancel it completely, delete it on the
Creator Dashboard Credentials page.
