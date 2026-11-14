# Setup guide (no coding needed)

## Easiest: the Windows app (no Python)

1. Download `RobloxAnalyticsAgent-windows.zip` from the
   [Releases page](https://github.com/Boking234/Rolytics/releases).
2. Unzip it somewhere permanent (keep both .exe files together) and double-click
   `RobloxAnalyticsAgent.exe`. If Windows says "Windows protected your PC", click
   **More info**, then **Run anyway** (the app isn't code-signed yet).
3. Follow the 6 setup screens: terms and age, sharing (off unless you turn it on), your Roblox
   key (with pictures), your game, your AI agent, and Discord (coming soon, so you can skip it).

That's it. The rest of this guide is for the command-line version.

## Command-line version

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
python -m pip install https://github.com/Boking234/Rolytics/archive/refs/heads/main.zip
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
Roblox game check-up, universe 123, data through Nov 12: Act now
1. ISSUE: Crash rate on Phone 3.8% vs usual 1.3% (+2.5 pts) on Nov 12. Whole game +0.9 pts.
   Roughly 14 more players hitting crashes a day. Confidence high, early estimate.
2. GOOD: Day 7 retention climbing for 12 weeks: 6.6% vs 5.6% (+1.0 pts). ...
```

After that, just run `roblox-agent` once a day. It fetches what's new and shows the top 3.
The first check-up of each week also shows a short weekly check-up.
Roblox deletes performance data after 28 days, so run it at least weekly.

Tell it what you did about an item (the number is the item's place in today's list):

| Command | What it does |
| --- | --- |
| `roblox-agent mark 1 done` | You fixed it. Hidden for 3 days, then it comes back only if it's still happening. |
| `roblox-agent mark 1 snooze` | Not now. Hidden for 7 days. |
| `roblox-agent mark 1 wrong` | False alarm. Hidden for 30 days, and that number needs stronger evidence from now on. |
| `roblox-agent why` | Why player numbers are rising or falling: fewer new players or players coming back less, with clues. |
| `roblox-agent change "Shorter tutorial"` | You shipped a change. 7 quick questions score how heavy it is; it gets judged after 3 to 21 days. |

How the checks decide what to show: [HOW_CHECKS_WORK.md](HOW_CHECKS_WORK.md).

If the key isn't accepted, check that it has your game and `universe.analytics:read`, and that
the IP setting allows `0.0.0.0/0`. Then run `roblox-agent setup` again.

## 7. Use it with your AI agent

If you skipped step 3, run `roblox-agent connect` any time and pick your agent. Restart the
agent, then ask it: **"Run my Roblox daily check-up."**

- The agent receives only the short check-up text (about 100 to 350 tokens, plus about 400 for
  the tool list once per session: well under 1% of reading a 1 MB file). It never sees your raw
  data unless you ask for a metric's history.
- You can also tell the agent "I fixed the phone crashes" or "log the shop change I shipped
  today", and it will mark the item or score the change for you.
- Your API key is never put in the agent's settings and never sent to the agent.

## 8. More views

| Command | What you get |
| --- | --- |
| `roblox-agent view` | The check-up as a page, with every chart under "All metrics" |
| `roblox-agent demo` | The same with made-up numbers, including a pretend crash spike |
| `roblox-agent week` | The weekly check-up, any day |
| `roblox-agent tune` | Shows how the checks are tuned. `tune sensitivity low` gives fewer, surer items; `high` gives earlier warnings. `tune limit ClientFpsP10 min 25` sets your own limit. |
| `roblox-agent selftest` | Plants known problems in pretend games and shows the checks find them |
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
