# Learned Feedback (mirror of Claude Code memory)

This file mirrors the writing and strategy feedback Muneeb has given Claude over time, captured in Claude Code's persistent memory. The cloud routines cannot read that memory directly (it lives outside this repo and is not on the GitHub Actions runner), so the relevant rules are mirrored here and folded into the voice context by load_brain(), which means every eToro routine reads these learned rules before drafting. When a new writing, voice, or eToro lesson is recorded in memory, mirror it here too.

Last synced: 2026-06-13

## Market recaps and weekly posts
- Capture the week's ARC, not just the net move. Pull the intraweek path (biggest up day, biggest down day, range). If the week round-tripped (sold off then recovered, or the reverse), say so explicitly. A flat weekly percentage hides a volatile week.
- Lead with the single biggest event of the period. Never bury a historic or market-moving event (a record IPO, a central-bank surprise, a greater-than-3 percent index day, a geopolitical shock) inside a cluster paragraph.
- Describe cluster moves concretely (which named holdings led or lagged, whether the cluster round-tripped). Do not hedge with "the portfolio likely traded broadly in line", that is say-nothing phrasing. Leave only the exact P/L as a [USER CONFIRM] placeholder.
- Before treating something as a major event, verify it actually happened and the facts are right. Source all macro, market, and sentiment numbers before drafting.

## Voice and register
- Open with "Hi Everyone," then a blank line. No sign-off, end on the last substantive line.
- Dual register: personal and conversational for actions and views, neutral reporting for market events. Use "we" and "our portfolio" for actions and ownership, "I" for beliefs and principles.
- Vary the opening. Do not start consecutive posts with the same formulaic phrase (for example "A note on..."). Open with content, not a meta-introduction.
- Plain, conversational language. No scaffolding lines, no theatrical endings.

## Hard formatting rules
- NO em dashes anywhere. Use commas, periods, parentheses, or colons.
- Plain text only. No markdown bold, no headers inside the post body. The post must be copy-ready straight into eToro.
- Maximum 5,000 characters.
- Write "portfolio", never "book", in anything user-facing.

## Tickers and numbers
- Use $TICKER throughout. A short informal name once on first mention is fine (for example $GOOG (Alphabet)). Do NOT write full legal entity names ("NVIDIA Corporation", "Advanced Micro Devices Inc"), that is eToro auto-decoration at publish, not the draft voice.
- No specific price levels for our own adds. Strip dollar accumulation zones and position-size percentage disclosures. Frame on technicals, relative strength, or qualitative weakness instead. Internal levels stay in portfolio.md only.
- Position state (percent cost, percent invested, P/L) sources from portfolio.md, never inferred or invented.

## Continuity
- Do not rehash. Read the published log. Do not re-lead a topic the recent Tuesday thesis, Sunday Week Ahead, or last recap already covered.
- When given a reference template, replicate its coverage scope (what gets touched), not its layout. Output reads as Muneeb's voice in a fuller format, not a clone.

## Stances (do not contradict)
- $TSLA: bullish on optionality, held. Never frame as "sized cautiously" or hedged against execution risk.
