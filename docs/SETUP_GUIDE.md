# Setup guide (no coding needed)

This takes about 15 minutes. You'll install Python, install the tool, make a Roblox
API key, and pull your game's numbers. Everything stays on your computer.

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

A page opens in your browser with charts made from made-up numbers. This shows you what the
real report will look like. Nothing is sent anywhere.

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

## 6. Set up and test

```
roblox-agent setup
```

1. Paste the Universe ID (or the dashboard link), press Enter.
2. Paste the API key, press Enter. **It won't appear on screen while you paste.** That's normal.
3. The tool tests the key and prints the last 7 days of daily active users.

If it says the key wasn't accepted: check the key has your game and `universe.analytics:read`,
and that the IP setting allows `0.0.0.0/0`. Then run `roblox-agent setup` again.

## 7. Pull your data

```
roblox-agent pull
```

The first run downloads a year of player, retention, and revenue history plus the last 27 days
of performance data (Roblox deletes performance data after 28 days, so run `pull` at least
once a week; daily is best). It can take a few minutes. Later runs only fetch what's new.

## 8. Look at it

| Command | What you get |
| --- | --- |
| `roblox-agent view` | A page in your browser with a chart per metric |
| `roblox-agent show` | A quick table in the terminal |
| `roblox-agent export` | A CSV file you can open in Excel or Google Sheets |
| `roblox-agent where` | Where your data and settings are saved |

## 9. Check it against the Roblox dashboard

Open your game on the Creator Dashboard, go to **Analytics**, and set the date range to the
last 7 days. Then compare with `roblox-agent view`:

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
