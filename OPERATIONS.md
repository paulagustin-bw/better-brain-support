# Operating the BetterBrain support bot

Everything here runs on the **mac-mini** (`pagustin`) as a proof of concept.
Nothing is on Betterworks infrastructure yet. Written 2026-07-22.

## What is running

Three launchd agents, all user-level (`~/Library/LaunchAgents`). User agents only
run while that user has a **logged-in GUI session** — if the mini is rebooted and
nobody logs in, none of this starts. That is the single most likely cause of
"the bot is dead and nothing is in the log."

| Label | What it does | Schedule |
|---|---|---|
| `com.betterbrain.support-bot` | The Slack bot itself, on `127.0.0.1:3000` | KeepAlive |
| `com.betterbrain.support-bot-tunnel` | `cloudflared` → `betterbrain-bot.paulagustin.com` | KeepAlive |
| `com.betterbrain.aha-daily-delta-refresh` | Aha delta → corpus PR (repo: `betterbrain`) | 07:30 daily |

`AUTOMATIONS.md` in the corpus repo says 9:15 for the third job. **The plist is
authoritative**; the doc is stale and has not been reconciled.

## Health check

```sh
launchctl list | grep betterbrain          # all three present?
lsof -nP -iTCP:3000 -sTCP:LISTEN           # bot actually bound?
tail -50 ~/Library/Logs/BetterBrainSupport/support-bot.log
```

The bot binds **loopback only**. Public reach is via the tunnel, so if Slack
events stop arriving but `lsof` shows the bot up, suspect the tunnel first.

## Restart

```sh
launchctl kickstart -k gui/$(id -u)/com.betterbrain.support-bot
```

`-k` kills first. Give it ~8 seconds before checking the port — it exits
non-zero briefly during startup and KeepAlive will relaunch it, so an immediate
check reads as failure when it is just slow.

## Logs

| Path | What |
|---|---|
| `~/Library/Logs/BetterBrainSupport/support-bot.log` | bot stdout+stderr |
| `~/Library/Logs/BetterBrainSupport/cloudflared-tunnel.log` | tunnel |
| `~/Library/Logs/BetterBrainSupport/usage.jsonl` | one row per `claude -p` call |
| `~/Library/Logs/BetterBrain/aha-daily-delta-refresh.log` | corpus automation |

## The two LLM paths — do not confuse them

This has caused a wrong fix at least once. There are **two** separate ways the
bot talks to a model, and they are governed by different files:

1. **The cascade** — `run_betterbrain_cascade()` in `src/slack/handler.py`
   shells out to `claude -p` with the `betterbrain-ask` skill. This handles Guru
   declines and direct @mentions. Its output contract lives **at the call site**,
   not in a persona file.
2. **ProductLens** — `src/agent.py`, governed by
   `.github/agents/ProductLens.agent.md` and `POLICY`. This is the
   codebase-Q&A path for channels with a `project` configured.

If cascade answers are too long or badly formatted, editing the persona file
will do nothing. Fix the contract in `run_betterbrain_cascade`.

## Model and cost

The cascade model is `BETTERBRAIN_CASCADE_MODEL`, defaulting to `sonnet`
(switched from the default tier 2026-07-22). The decline classifier is on Haiku.

```sh
python3 scripts/usage_report.py               # spend vs gap-log outcomes
python3 scripts/usage_report.py --by-model    # compare after a model switch
```

Baseline before the Sonnet switch: 18 cascade runs at **$1.75 each**, 25
classifier runs at $0.03. $1.01 per answered question, $1.70 per PKR-tied
answer. Those dollar figures are notional — the work is billed against a Max
subscription — but they are the right numbers to project an API bill from.

To move to Betterworks API keys, set `ANTHROPIC_API_KEY` in the bot's
environment. No code change. The signal to watch is **not** total spend but
concurrency: Max is a per-account limit shared with interactive sessions, so
collisions show up as timeouts before they show up as a bill.

## Per-channel behaviour (`config.yaml`)

- `cascadeReply: dm | thread` — where a cascade answer lands. Only
  `#tmp_betterbrain` is set to `thread`; everywhere else answers go to Paul by DM.
- `appMention: true | false` — whether `@BetterBrain` runs the cascade in that
  channel. **Currently `false` in `#product`, `#support`, `#engage_product`, on
  purpose.** The code path works; it is off because the cascade has too few
  evaluated answers to be trusted publicly (one of the first two was confidently
  wrong with plausible-looking sourcing). Turning it on is a one-line change and
  should happen only after ~12 clean runs in `#tmp_betterbrain`.

## Secrets

`.env` in the repo root, gitignored, loaded by the plist via `set -a; source`.
Never commit it and never echo it into a transcript. Aha credentials for the
corpus automation live separately in
`~/.config/betterbrain/aha-refresh.env`.

## Known constraints

- **GUI login required** (see above).
- The bot writes gap-log entries into the corpus repo through
  `scripts/append_gap.py`, which is deliberately narrow: it validates, refuses
  PKR-shaped fields, and only appends. The daemon can record what it could not
  answer; it cannot author product truth. Keep it that way.
- Slack request signature verification is implemented and was audited
  2026-07-21. If you touch request handling, re-check it.
