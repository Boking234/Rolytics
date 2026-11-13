# How the checks work (and how we know they work)

Everything here runs on your computer. Your AI agent only sees the short result.

## The daily check-up in one minute

Every day the tool looks at 16 numbers about your game (players, retention, money, crashes,
frame rate, memory, how many people are shown the game...), for the whole game and for each device
(Computer, Phone, Tablet, Console). For each one it asks four questions:

1. **Did it suddenly change?** It works out what today *should* look like: the game's level over
   the last two weeks, adjusted for the day of the week (Saturdays are busier than Tuesdays). Then
   it measures how far today is from that, compared with how much this number normally wobbles.
   One very unusual day is a *spike*. Two to seven days that are all off in the same direction are
   a *shift*, which catches a 20% drop that hides in one day's noise but not in a week's.
2. **Is it slowly sliding?** It averages each of the last 12 weeks and checks for a steady climb
   or fall (two different statistical tests have to agree, and the total change must be at least
   10%). This catches "Day 7 retention fell a little every week for two months".
3. **Did it drop and stay down?** It compares the last 4 weeks with the 4 weeks before, each day
   against the same weekday. This catches a drop from three weeks ago that the day-by-day check
   has stopped seeing because it became "the new normal". It also works out when it started
   ("dropped around Oct 20" or "sliding since about Oct 6") so you can match it to what you shipped.
4. **Is it past a rule-of-thumb limit?** For example phones under 20 fps on most days of the
   last week. That catches problems that are bad but "normal" for your game.

Then it tidies up, so you get 3 clear items instead of 15 alarms:

- **One story, told once.** If crashes rose on every device but mostly on phones, you get one
  item, "Crash rate on Phone", with the whole-game number as a note. "Players down" and "visits
  down" become one item with an "Also:" line.
- **Size in players or Robux.** "Roughly 14 more players hitting crashes a day", "2,300 Robux a
  day less". Tiny problems (under 5 players a day) are counted but not shown.
- **Confidence.** High, medium or low. "Low" means it may well be noise, so it is counted but
  not shown. Days Roblox marks as an estimate lower the confidence of one-day blips.
- **More players is not a problem.** Crash and out-of-memory counts are compared after allowing
  for how many people played, so a viral day doesn't look like a crash wave.
- **Ranking.** Issues first, by how much the number matters (crashes and retention most) x how
  big the change is x how many players it reaches x confidence.
- **Status.** "Act now" only for a big, confident problem in crashes, players, retention or
  money that affects real numbers of players. Any other problem is "Watch".

## It remembers

- An ongoing problem says "day 3 on this list" and slowly sinks so new news can surface. It never
  disappears while it lasts.
- `roblox-agent mark 1 done` hides item 1 for 3 days while your fix takes effect. If it's still
  there after that, it comes back saying "still there after you marked it fixed".
- `mark 1 snooze` hides it for 7 days. `mark 1 wrong` records a false alarm and hides it for 30.
- Your agent can do the same through the `mark_item` tool ("I fixed the phone crashes").

## Changes you ship, and the weekly check-up

`roblox-agent change "Shorter tutorial"` asks 7 quick questions from the change-weight rubric
(how many players it touches, how many systems, knock-on effects, economy, paid items, progress,
and how easy it is to undo). The total (0-21) sets the class and how long to wait before judging:
Light 3 days, Medium 7, Heavy 14, Major 21. A 3 in "paid items" or "undo" makes it at least Heavy.
When the wait is over, the daily check-up says once whether it looks good, harmful, or unclear,
and names any other change that overlapped.

The first check-up of each week adds a short weekly check-up: a health scorecard (stability,
performance, players, engagement, retention, monetization) vs last week and the 4 weeks before,
the issues that are new, still going or fixed, your changes and their verdicts, and the top 3 to
work on next week.

## Slow declines, longevity, and "why are fewer people playing?"

**Lifecycle.** Once a week (and whenever you ask) you get one line on where the game is in its
life: new, growing, at its peak, steady, recovering, declining or fading. It says how far the
game is below its best 4 weeks, how fast it is shrinking, and when it would halve at that pace.
Example: "Lifecycle: declining. 1,026 players a day, 80% of the best 4 weeks, -13% vs the 4 weeks
before. Losing about 3.9% a week; at this pace players halve in about 17 weeks."

**Why.** Player numbers can only fall for two reasons: fewer new players arrive, or the players
you have come back less. `roblox-agent why` (or asking your agent "why are players down?")
splits the drop into those two, using numbers Roblox already gives per day:

- *players today = new players today + returning players today*
- *come-back rate = returning players today / all players yesterday*

Then it says which one is the main cause, how many new players a day you would need to hold the
old level, and lists up to 4 clues: Day 1, Day 7 and Day 30 retention, session length, how many
people are shown the game and how many of them start playing, crashes and frame rate. It also
says whether one device holds most of the loss, and names any change you logged around the day
it started. A player drop on the daily list carries the short version ("Mostly fewer new
players (-26%)").

## It adapts to your game

- **It starts from real games, not a guess.** How much a number wobbles from day to day depends
  on the game's size: a 50-player game swings 30% for no reason, a 50,000-player game barely 6%.
  The starting values were measured on public data (see below). As your game collects history
  the tool leans on that instead, and after about 5 weeks it uses only your game's own normal.
- **It learns from your verdicts.** Each `mark N wrong` makes that number need stronger evidence
  next time (up to three steps). Each problem you confirm with `mark N done` wins half a step back.
- **One dial.** `roblox-agent tune sensitivity low` shows fewer, surer items; `high` warns
  earlier and is wrong more often.
- **Your own limits.** `roblox-agent tune limit ClientFpsP10 min 25` replaces a rule of thumb
  with your own target.
- **Checks are building blocks.** Each check (sudden change, slow slide, 4 weeks vs 4 weeks,
  limit) is a small separate piece in one list. A new check can be added, or one switched off
  (`tune off slide`), without touching the others. Later stages (the community database) can add
  checks and better starting values the same way.

## Getting smarter over time (new in 0.5)

Five things now improve the suggestions without any extra users and without an AI model. All of
it is plain math on your computer.

**1. It learns from your clicks.** Fixed, Snooze and Not a problem are labels. Items like the ones
you usually act on move up the top 3; items like the ones you usually dismiss or snooze move down.
It learns per number (e.g. crash rate) and per kind of check (sudden change, slow slide, rule-of-
thumb limit). Safety rules: it never hides an item, never changes the status ("Act now" stays
"Act now"), starts neutral, one click moves it only a little, and labels older than 6 months stop
counting. False alarms still also raise the evidence bar, as before.

**2. It learns from the changes you ship.** Every judged change keeps how it moved each number.
Once two or more similar changes are judged, it spots patterns like "2 of your 3 economy changes
lowered paying user conversion (-13% on average)". You see these when you log a similar change,
in the weekly check-up, and as a clue when a number moves after a change. If your "Light" changes
turn out harmful at least half the time (2 or more), new Light changes are judged after 7 days
instead of 3.

**3. Likely causes.** Each problem gets one short "Likely cause", from the best supported of:
- *Player mix*: e.g. more players on phones, which run slower and crash more. The whole-game value
  is recomputed with the old per-device values and today's device shares; if that explains most
  of the move, no device actually got worse. These are never "Act now".
- *A wave of new players*: new players a day up 30%+ over the same days. First-time visitors come
  back and spend less, so retention and conversion dip for a while.
- *Discovery*: for fewer players, is Roblox showing the game to fewer people, or are fewer viewers
  pressing play (icon, thumbnail, title)?
- *A change you shipped* just before it started (timing is a clue, not proof, so it comes last).

Roblox's analytics API (as used here) gives no per-source split of players (search, home, ads,
friends), so "where players came from" is inferred from new-player and impression numbers.

**4. Something to try, then a check that it worked.** Each problem gets one "Try:" line picked
for the number, the device, sudden vs slow, and the likely cause (e.g. "roll back change #4 for a
day" or "test on a low-end phone"). Your AI agent turns it into specific steps. When you mark an
item Fixed, it remembers the normal level and the problem level, and a few days later (longer for
retention and slow problems) it says "the fix worked", "partly fixed" or "the fix didn't work
yet". A failed fix puts the item back with the next idea; a fix that worked is shown first next
time the same symptom appears. The check-up is still small: the longest test case is about 1,000
characters (about 260 tokens).

**5. It scores its own accuracy.** The self-test now also checks that the right cause is named
(a shipped update, a phone surge, a new-player wave, being shown less). The results are saved in
`accuracy.json`, and the automatic tests (which also run before every Windows build) fail if a new
version finds fewer planted problems, names fewer right causes, or raises more false alarms.
`roblox-agent selftest --mine` plants the same kinds of problems into a copy of your own game's
history, at four points in the past, and tells you how many it found. Your data is only read.

`roblox-agent learned` (and Settings > "What it has learned about your game" in the app) shows
everything learned so far.

## What was learned from public data

Roblox does not publish other games' retention, crashes or revenue, so there is no public
Roblox data to train the retention or crash checks on. What is public, and what was used:

| Public data (fetched 2026-11-13) | What it was used for |
|---|---|
| 39,000 Fortnite Creative islands: daily players, minutes played, peak players, Day 1 and Day 7 retention, 10 days. User-made games on another platform, published by Epic. | Measuring how much each number wobbles day to day at each game size. This became the starting guess for a new game. |
| 6,500 Roblox games: public player counts about every hour, 4 days. | Cross-check on Roblox itself. Roblox games with 70 to 14,000 players online wobbled 6 to 10% day to day, the same range as Fortnite islands of that size. So the size curve is used for Roblox, with a floor of 6%. |

Back-test on real games: for 386 islands with 10 full days, the first 8 days were treated as a
new game's whole history and the last day was scored. A well-tuned guess gives a typical score
near 0.67 at every game size. The old flat guess (15% for everyone) ranged from 0.31 to 4.0
depending on size, so it was far too jumpy for small games and too sleepy for big ones. The
size-based guess ranges from 0.6 to 2.0. For session length and Day 1 retention it is close to
right (0.6 to 0.9). For player counts it is still on the jumpy side (1.2 to 2.0), because real
games have many genuinely big days (10 to 20% of islands moved by a large amount on the last
day) and 10 days is too short to learn a weekly rhythm. Scripts are in `research/`.

## How this compares with the big analytics tools

| What they do | Where it is here |
|---|---|
| **GameAnalytics** anomaly detection: an expected range per metric, a confidence dial (70 to 99%), needs 30 days of history. | Expected value per weekday plus normal wobble; the sensitivity dial; works from about 2 weeks of history because it starts from public-data values. Adds size in players, device, and ranking, which GameAnalytics does not. |
| **Roblox Creator Dashboard**: compares with 28 days earlier, 7-day averages, benchmarks against similar games, new vs returning player segments. | The 4-weeks-vs-4-weeks check, weekly averages in the scorecard, new vs returning split in "why". Benchmarks: only a rough guide here; Roblox's own benchmark is better and stays the reference. |
| **Amplitude Lifecycle** and investor **growth accounting** (new, returning, resurrected, lost users; "quick ratio"). | The new-players vs come-back-rate split, and the "needs X new players a day, gets Y" line. Roblox's daily numbers don't separate players who return after a long break, so that part is not possible yet. |
| **Statistical process control** (how factories and large live-service teams watch metrics): alarms need both statistical evidence and a minimum size; run tests for small sustained shifts. | The same two-part rule on every check, and the "2 to 7 days all off the same way" shift test. |

## How we know it works: the self-test

**Version 0.5 (2026-11-13):** three cause tests were added (an update that breaks Day 1
retention, phone players tripling, a wave of new players), making 28 situations. With 10 pretend
games each: 194 of 200 planted problems found, 39 of 40 right causes named, 78 of 80 calm traps
kept calm, 3 of 80 calm days raised any issue and none said "Act now". The 25 older situations
score exactly as in 0.4 (165 of 170, 78 of 80), so nothing got worse. The table below is from 0.4.

`roblox-agent selftest` builds pretend games on your computer, plants known problems in them,
and checks the daily check-up names them. The pretend games are deliberately hard: busy weekends,
small games that are very noisy, rates that depend on how many players are behind them,
retention that isn't known yet for the last days, low-traffic devices marked "too few players",
and Roblox sending percentages instead of fractions.

Results on 2026-11-13 (10 pretend games per problem, 300 calm days). The test now has 25
situations instead of 20: the new ones are a drop that stayed for three weeks, new players
drying up, players coming back less, and a game with only 16 days of history (once with a crash
problem, once calm).

| | First version | Stage (b) | Now |
|---|---|---|---|
| Planted problems found (with the right device, "Act now" where it should be, and the right reason for "why") | 83 of 130 | 125 of 130 | **165 of 170** |
| Calm trap cases kept calm | 44 of 70 | 70 of 70 | **78 of 80** |
| Calm days that raised any issue | 34% | 3% | **3%** |
| Calm days that said "Act now" | 26% | 0% | **0%** |

The 4 new problem situations were found in all 10 pretend games each, and "why" named the right
cause every time. The 2 misses among the calm cases are both in the 16-day-old game: with so
little history a lucky or unlucky couple of days still looks like news. Those items are labelled
"Confidence low" and the check-up says it is still learning.

Every crash spike, players drop, broken shop, frame-rate drop, memory leak and server-crash jump
was found in all 10 pretend games. The misses: a 25% Day 1 retention drop that had only lasted 4
days (9 of 10 found; the 10th is caught a few days later), and slow slides in Day 7 retention
(7 of 10) and players (9 of 10). Day 7 retention is very noisy in a game with ~1,200 players a
day, so a 3%-a-week slide only becomes certain after more weeks.

## What is not proven yet

- **No real Roblox game's private numbers have been tested yet.** The pretend games are built to
  behave like real ones, and the starting wobble values now come from real public data, but real
  games have surprises (holidays, Roblox outages, events). Expect to tune once a real key is
  connected. `mark 1 wrong` now feeds that tuning automatically.
- **The public data is mostly Fortnite, not Roblox, and only 10 days long.** It was used for one
  thing: how noisy numbers are at each game size. Roblox player counts agreed with it on the two
  days that could be compared. Crashes, frame rate, memory and revenue have no public data at all
  and keep a cautious starting guess.
- **"Why" is a split, not proof.** It shows which part moved (new players or come-back rate) and
  what moved with it. It cannot see why fewer people are shown the game (Roblox's algorithm, a
  rival game, the end of a holiday).
- **The 5 new metric names** (Day 30 retention, monthly players, stickiness, people who saw the
  game, saw-it-to-played-it rate) were taken from Roblox's documentation on 2026-11-13 and have
  not been pulled with a real key yet. If Roblox refuses one, the pull skips it and says so.
- **Units.** We don't yet know whether Roblox sends retention as 0.21 or 21. The checks accept
  both and decide per metric.
- **"Projected" days.** We treat Roblox estimates carefully but haven't seen how far real
  estimates move.
- **Change verdicts are before/after comparisons, not A/B tests.** Something else that happened
  at the same time (a holiday, a YouTuber) can be the real cause. The verdict names overlapping
  changes; true A/B testing comes with the experiments stage.
- **Likely causes and "Try" lines are starting points.** They were tested on pretend games
  (7 of 8 causes right in the standard run; the miss was a problem that wasn't detected at all),
  not on a real game yet. The fix ideas are common Roblox advice, not a diagnosis of your game.
- **Learning needs your clicks.** Until you've marked a handful of items and logged a few changes,
  nothing has been learned and everything behaves as in 0.4.
- **Rule-of-thumb limits** (phones under 20 fps, servers under 40 fps, crash rate over 3%) are
  starting points, not Roblox official numbers.
