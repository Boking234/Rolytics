# UI research for the desktop app redesign

Read 2026-11-17 before redesigning the app's screens. Each source lists what it says and what
we changed because of it. Screenshots of the old and new screens are in the pull request that
added this file.

## Sources

| # | Source | What it says (short) | What we did |
|---|---|---|---|
| 1 | [5 Principles of Visual Design in UX](https://www.nngroup.com/articles/principles-visual-design/), Kelley Gordon, NN/g | Use no more than about 3 element sizes; make the most important thing the biggest. Things close together read as one group. Don't lower contrast to de-emphasise. | The day's status ("Act now") is the only large text. Body is 15px, labels 11.5 to 13px. Grouping comes from spacing and rules, not boxes. Muted text still passes 4.5:1. |
| 2 | [Visual Hierarchy in UX: Definition](https://www.nngroup.com/articles/visual-hierarchy-ux-definition/), Kelley Gordon, NN/g | Saturated colour for what matters, muted for the rest; keep warm reds for warnings. Use containers only when spacing isn't enough. Do the squint test. | Colour is used only for the three states (red act, amber watch, green good). Everything else is ink on paper. |
| 3 | [10 Usability Heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/), Jakob Nielsen, NN/g | Show system status, give undo and a clear exit, prefer recognition over recall, keep it minimal. | Setup shows every step by name in a side rail, so you can see where you are and what's left. Undo stays on every marked item. |
| 4 | [Cards: UI-Component Definition](https://www.nngroup.com/articles/cards-component/), Page Laubheimer, NN/g | Cards suit mixed, browsable content. For similar items use a list; cards hide ranking and make comparing harder. | The top 3 are now a numbered list (01, 02, 03), because they are similar and ranked. Inbox is a list too. |
| 5 | [Summary of key rules](https://practicaltypography.com/summary-of-key-rules.html), Matthew Butterick | Web body text 15 to 25px, line spacing 120 to 145%, 45 to 90 characters per line, 5 to 12% extra letterspacing on all caps, avoid default system fonts. | 15px body at 1.55, paragraphs capped at 68 characters, uppercase labels at 8% tracking, and a real typeface (IBM Plex) bundled with the app. |
| 6 | [Designing accessible color systems](https://stripe.com/blog/accessible-color-systems), Koopersmith and Miner, Stripe | 4.5:1 contrast for small text, 3:1 for large. Pick colours in a perceptual model, not HSL. Accessible doesn't have to mean bright. | Status colours were darkened until they pass on the paper background in light mode and lightened for dark mode. |
| 7 | [How we redesigned the Linear UI (part II)](https://linear.app/now/how-we-redesigned-the-linear-ui), Karri Saarinen and team, Linear | Build themes from a few inputs (base, accent, contrast); keep chrome neutral so it doesn't compete with content; the UI must work as a native window and in a browser. | All colours are a dozen tokens with a light and dark set. The chrome (tabs, header, buttons) is neutral ink. Works the same in the window and the browser. |
| 8 | [7 Signs a UI Has Been Vibe Coded](https://www.thefountaininstitute.com/blog/signs-vibe-coded-ui), Jeff Humble, The Fountain Institute | Signs: purple-blue gradients, emoji as icons, cards inside cards, coloured left bars on every block, status dots that mean nothing. Use one dominant colour, one accent, one neutral. | Removed the coloured left bars, the pill badges, the 3-box feature grid and the check-mark glyphs. Each coloured square now means exactly one state. |
| 9 | [Why every AI-built website looks the same](https://dev.to/alanwest/why-every-ai-built-website-looks-the-same-blame-tailwinds-indigo-500-3h2p), Alan West, DEV | The "median" AI page: Tailwind indigo/blue accent, Inter or system font, 0.5rem radius everywhere, soft shadows, three icon boxes. Fix it by choosing palette, font and layout on purpose. | Dropped the Tailwind blue (#2563eb) and its tints, the shadows and the 12px radius. Picked warm paper, ink, 6px corners and IBM Plex Sans and Mono. |
| 10 | [Why your AI keeps building the same purple gradient website](https://prg.sh/ramblings/Why-Your-AI-Keeps-Building-the-Same-Purple-Gradient-Website), prg.sh | Avoid Inter/Roboto/Arial; pair fonts with contrast; use one dominant colour with sharp accents; avoid "three boxes with icons". | Sans for reading, mono for numbers, dates, labels and the step rail, so data reads as data. |
| 11 | [Hick's Law](https://lawsofux.com/hicks-law/), Jon Yablonski, Laws of UX | More choices means slower decisions; break tasks into steps and highlight the recommended option. | One primary (filled) button per screen. The three item actions are quiet text buttons; "Details" sits apart from them. |
| 12 | [Doherty Threshold](https://lawsofux.com/doherty-threshold/), Jon Yablonski, Laws of UX | Respond within 400 ms; progress bars make waits feel shorter. | Hover and press feedback in 120 ms, no loading animations, a thin progress bar while the first report is made. |

Also tried: Refactoring UI's "7 practical tips for cheating at design" (Wathan and Schoger), but
Medium refused the request, so it isn't counted above.

## The rules we ended up with

1. **Ink on warm paper.** Colour is spent only on meaning: red is act now, amber is watch, green is good news.
2. **Three sizes do the work:** 11.5 to 13px labels, 15px reading text, 30px for the day's status.
3. **Lists for similar things, boxes only for groups** (settings sections, the key form).
4. **Numbers in mono.** The change in each item title ("−22%") is set in IBM Plex Mono in its state's colour, with a real minus sign.
5. **One filled button per screen.** Everything else is outlined or plain text.
6. **No decoration that means nothing:** no gradients, shadows, emoji, coloured side bars or pill badges.

The fonts are IBM Plex Sans and IBM Plex Mono (SIL Open Font License, `app/web/FONTS-OFL.txt`),
latin subset, about 100 KB in total, served from the app so it works offline.
