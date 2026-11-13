# Design notes: how top apps keep users, and what we took

Researched 2026-11-13. Sources are linked. **Applied** means it's in the tool now, and
**Later** means it's planned for the app or Discord stage.

| # | Pattern (who does it) | Evidence | How we use it |
|---|---|---|---|
| 1 | **One daily status plus a few reasons** (Oura Readiness, WHOOP green/yellow/red) | [Oura](https://ouraring.com/blog/readiness-score/), [WHOOP](https://developer.whoop.com/docs/whoop-101) | **Applied:** one status (All good / Watch / Act now) plus the top 3 changes. |
| 2 | **Progressive disclosure**: the main screen shows only what matters, and details sit one click away (NN/g) | [NN/g](https://www.nngroup.com/videos/progressive-disclosure/) | **Applied:** the page shows status and top 3. All charts are folded under "All metrics". There are 4 main commands, and the rest are marked advanced. |
| 3 | **Opinionated defaults, no settings** (Linear) | [Linear method](https://www.figma.com/blog/the-linear-method-opinionated-software/) | **Applied:** fixed metrics and thresholds, compared with the same weekday, and nothing to configure. |
| 4 | **Fast first value** | [Userpilot TTV benchmark](https://userpilot.com/blog/time-to-value-benchmark-report-2024) | **Applied:** setup ends by showing your first real check-up. `demo` shows the result before you have a key. |
| 5 | **Short, guided, mandatory setup** (Superhuman: completion went from about 30% to 98% once setup was mandatory) | [First Round](https://review.firstround.com/superhuman-onboarding-playbook/) | **Applied:** "Step 1 of 3" wizard (game, key, AI agent). Running with no command starts it automatically. |
| 6 | **Helpful empty states** (NN/g) | [NN/g](https://nngroup.com/articles/empty-state-interface-design) | **Applied:** no data shows one next step. A calm day says "No unusual changes across N metrics". |
| 7 | **Streak on a tiny daily action** (Duolingo: a 7-day streak makes completion 3.6x more likely) | [Duolingo](https://blog.duolingo.com/how-duolingo-streak-builds-habit) | **Later:** a check-up streak with free "freeze" days and no paid repairs. |
| 8 | **Hook loop: trigger, action, reward, investment** (Nir Eyal) | [Amplitude](https://amplitude.com/blog/the-hook-model) | **Applied (0.2):** `mark 1 done / snooze / wrong` is the investment: fixed items are re-checked after 3 days, snoozed ones hide for 7, false alarms for 30. **Later:** the Discord DM as the trigger. |
| 9 | **Few, smart notifications** (about 10% of people turn off push at 1 per week, and about 40% at 3 to 6) | [Business of Apps](https://www.businessofapps.com/marketplace/push-notifications/research/push-notifications-statistics/) | **Later:** at most one DM a day, sent only when there's news, and it backs off if ignored. |
| 10 | **Celebrate real outcomes, not taps** (Robinhood removed confetti after regulators objected) | [CNBC](https://www.cnbc.com/2021/03/31/robinhood-gets-rid-of-confetti-feature-amid-scrutiny-over-gamification.html) | **Applied:** "GOOD" items are real metric gains, such as retention up 25%. |
| 11 | **One main action per issue** | Combines patterns 2, 3 and 5 | **Applied (basic):** the agent's prompt asks for one next step per issue. **Later:** a "Send to my agent" button. |

**Ethics.** Many Roblox developers are teenagers. Following the UK Children's Code on nudges
([ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/childrens-information/childrens-code-guidance-and-resources/age-appropriate-design-a-code-of-practice-for-online-services/13-nudge-techniques/))
and the FTC dark-patterns report, the tool will never use:
- guilt-trip messages
- fake urgency
- paid streak repairs
- hidden data sharing

Pausing check-ups will always be one click.

## Token budget for the daily check-up

All analysis runs locally in Python. The AI agent receives only:

| What | Size |
|---|---|
| Tool list (5 tools, sent once per session) | ~1,700 characters, about 400 tokens |
| Daily check-up text | ~300 to 1,400 characters, about 100 to 350 tokens |
| Weekly check-up (first check-up of a week only) | adds ~400 to 1,000 characters, about 100 to 250 tokens |
| Built-in prompt | ~300 characters |
| **Total for one check-up** | **about 600 to 850 tokens (up to ~1,100 on the weekly day)** |

A 1 MB file is about 250,000 tokens, so a check-up costs about 0.3% of that. Automated tests fail if
the daily check-up passes 1,500 characters in any test scenario, the weekly day passes 2,600, or
the tool list passes 3,000. The tool list grew in 0.2 (two new tools: mark an item, log a change).
Your agent's own built-in instructions cost extra, and that part depends on the agent, not on
this tool.
