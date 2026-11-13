# How the checks work (and how we know they work)

Everything here runs on your computer. Your AI agent only sees the short result.

## The daily check-up in one minute

Every day the tool looks at 14 numbers about your game (players, retention, money, crashes,
frame rate, memory...), for the whole game and for each device (Computer, Phone, Tablet, Console).
For each one it asks three questions:

1. **Did it suddenly change?** It works out what today *should* look like: the game's level over
   the last two weeks, adjusted for the day of the week (Saturdays are busier than Tuesdays). Then
   it measures how far today is from that, compared with how much this number normally wobbles.
   One very unusual day is a *spike*. Two to seven days that are all off in the same direction are
   a *shift*, which catches a 20% drop that hides in one day's noise but not in a week's.
2. **Is it slowly sliding?** It averages each of the last 12 weeks and checks for a steady climb
   or fall (two different statistical tests have to agree, and the total change must be at least
   10%). This catches "Day 7 retention fell a little every week for two months".
3. **Is it past a rule-of-thumb limit?** For example phones under 20 fps on most days of the
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

## How we know it works: the self-test

`roblox-agent selftest` builds pretend games on your computer, plants known problems in them,
and checks the daily check-up names them. The pretend games are deliberately hard: busy weekends,
small games that are very noisy, rates that depend on how many players are behind them,
retention that isn't known yet for the last days, low-traffic devices marked "too few players",
and Roblox sending percentages instead of fractions.

Results on 2026-11-13 (10 pretend games per problem, 300 calm days), compared with the first
version of the check-up from stage (a):

| | First version | Now |
|---|---|---|
| Planted problems found (with the right device, and "Act now" where it should be) | 83 of 130 | **125 of 130** |
| Calm trap cases kept calm (weekends, tiny game, big game, an old glitch, a fixed bug, percent units) | 44 of 70 | **70 of 70** |
| Calm days that raised any issue | 34% | **3%** |
| Calm days that said "Act now" | 26% | **0%** |

Every crash spike, players drop, broken shop, frame-rate drop, memory leak and server-crash jump
was found in all 10 pretend games. The misses: a 25% Day 1 retention drop that had only lasted 4
days (9 of 10 found; the 10th is caught a few days later), and slow slides in Day 7 retention
(7 of 10) and players (9 of 10). Day 7 retention is very noisy in a game with ~1,200 players a
day, so a 3%-a-week slide only becomes certain after more weeks.

## What is not proven yet

- **No real Roblox data has been tested yet.** The pretend games are built to behave like real
  ones, but real games have surprises (holidays, Roblox outages, events). Expect to tune once a
  real key is connected. `mark 1 wrong` is there partly to collect that feedback.
- **Units.** We don't yet know whether Roblox sends retention as 0.21 or 21. The checks accept
  both and decide per metric.
- **"Projected" days.** We treat Roblox estimates carefully but haven't seen how far real
  estimates move.
- **Change verdicts are before/after comparisons, not A/B tests.** Something else that happened
  at the same time (a holiday, a YouTuber) can be the real cause. The verdict names overlapping
  changes; true A/B testing comes with the experiments stage.
- **Rule-of-thumb limits** (phones under 20 fps, servers under 40 fps, crash rate over 3%) are
  starting points, not Roblox official numbers.
