# cc-statusline

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)
![Platform: macOS | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

A status line for [Claude Code](https://claude.com/claude-code). It shows how full your
context window is, what the current session and the current project have cost, and how much
of your 5-hour and 7-day limits you have used.

![cc-statusline running in a Claude Code session](assets/s1.png)

It is one Python file with no dependencies beyond `python3`. It reads the transcripts Claude
Code already writes to disk, prices them with the public LiteLLM rate table, and updates that
table once a day on its own. There is no cron job to set up and nothing to install alongside it.

> [!IMPORTANT]
> **Every cost and token count here is an estimate**, worked out on your own machine at API
> list prices. On a Pro or Max subscription this is not what you pay — it is what the same
> usage would have cost through the API. See
> [Costs and tokens are estimates](#costs-and-tokens-are-estimates) for the full list of caveats.

## What it shows

- **Context window** — how full it is right now, matching the percentage Claude Code reports.
- **Session** — cost, tokens, and how long the session has been running.
- **Project** — what this working directory has cost in total, and inside each limit window.
- **Account** — what every session on the account spent inside the 5-hour and 7-day windows.
- **Limits** — how much of each window you have used, and when it resets.

## Install

**1. Get the file**

```sh
curl -O https://raw.githubusercontent.com/lotyszm/cc-statusline/main/statusline.py
```

Or clone the repository:

```sh
git clone https://github.com/lotyszm/cc-statusline.git
cd cc-statusline
```

**2. Run the installer**

```sh
python3 statusline.py --install
```

It does three things:

- finds every Claude Code account directory in your home folder (`.claude`, `.claude-work`, …)
- backs up each `settings.json`, then adds the `statusLine` entry to it
- downloads the price table once, so the very first render already has real rates

A folder counts as an account if it contains a `projects/` folder or a `settings.json`, so an
account you have set up but never used is found as well. To choose the folders yourself, pass
them as arguments:

```sh
python3 statusline.py --install ~/.claude-work
```

**3. Restart Claude Code.** It reads `settings.json` only at startup.

> [!NOTE]
> `--install` writes the absolute path of the file into `settings.json`. Keep `statusline.py`
> where it is, or run `--install` again after moving it.

### Requirements

- Python 3.8 or newer — the one that ships with macOS or your distribution is fine
- a terminal with 256-colour support
- macOS or Linux (Windows is untested)

### Manual setup

If you would rather not run the installer, add this to `~/.claude/settings.json` yourself:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 \"/absolute/path/to/statusline.py\"",
    "padding": 1,
    "refreshInterval": 30
  }
}
```

## Reading the status line

### The header

```
Opus 5 · max · ai/statusline · ⑂ main · project $5.66 / 5.04M
```

| Part | Meaning |
| --- | --- |
| `[claude-work]` | Account tag. Shown only when the account folder is not `~/.claude`. |
| `Opus 5` | The model in use. |
| `max` | Reasoning effort. |
| `ai/statusline` | Working directory, shortened to its last two parts. |
| `⑂ main` | Git branch, when you are inside a repository. |
| `project $5.66 / 5.04M` | What this project has cost in total, in dollars and tokens. Reads `new directory` when the project has no transcripts yet. |

Warnings are appended to the same line:

| Warning | Meaning |
| --- | --- |
| `⚠ no price data` | The price table has never been downloaded. |
| `⚠ prices 5d old` | No download has succeeded for 5 days. |
| `⚠ unpriced model` | A model used **in this project** is missing from the price table, so its rates are guessed. |

### The `ctx` row

```
ctx ████▒▒▒▒▒▒  42%  84k/200k   session $1.87 / 6.4M · 1h 15m
```

- The bar shows how full the context window is. It counts input, cache writes and cache reads
  together, which is how Claude Code works out its own percentage.
- `84k/200k` — tokens currently in the context window, and the size of the window.
- `session $1.87 / 6.4M` — cost and tokens for this session only.
- `1h 15m` — how long the session has been open.

### The `5h` and `7d` rows

```
7d  ██████▒▒▒▒  63%  ↻ 3d 11h   all $367.60 / 429.51M · project $88.21 / 96.3M
```

- The bar shows how much of the limit you have used. **This percentage comes from Claude Code
  itself**, not from the script's own maths.
- `↻ 3d 11h` — time left until the window resets.
- `all` — what every session on this account spent inside that window.
- `project` — what this project alone spent inside that window.

Every cost keeps its own token count right next to it, in `$cost / tokens` form, so a number
is never read against the wrong label.

Both windows are measured from the reset time Claude Code reports, rather than counted
backwards from the current moment. That way the figures cover the same period the limit
itself covers. If Claude Code sends no limit data — when you use an API key instead of a
subscription, for example — the bars read `limit n/a`.

### `all` versus `project`

The same account, two different projects, a few seconds apart:

![Status line in the first project](assets/s1.png)

![Status line in the second project](assets/s2.png)

`all $587.07 / 672.42M` is identical in both, because it covers the whole account. `project`
is not: the first project spent $5.66 inside the 7-day window, the second only $0.14 — even
though that second project has cost $2.47 in total, as its header shows. Most of its work
happened more than seven days ago.

### Readings shared between sessions

Claude Code sends limit data only alongside a session's own API responses. A session sitting
idle therefore keeps reporting the last percentage it happened to see, which can be well out
of date.

To work around this, every session on one account writes its readings to a shared file in the
cache folder, and the highest reading for the current window wins — usage inside a window
only ever grows.

- A `~` after the percentage means the number came from another session rather than this one.
- If a session was idle while a window reset, it takes over the newer window completely,
  reset time included, so its cost figures cover the window that is actually running.

### Narrow terminals

Below 95 columns the line drops its least important details so that it still fits: the token
count next to each cost, and the session duration on the `ctx` row.

Bars turn amber at 70% and red at 90%.

## Costs and tokens are estimates

Claude Code does not tell the script what anything costs. The script adds up the `usage`
blocks in your transcript files and multiplies them by public API list prices. Everything
below follows from that.

- **On a subscription these are not your costs.** Pro and Max are flat-rate plans. The figures
  show what the same usage would have cost through the API.
- **"Project" means one directory.** Claude Code stores transcripts per working directory
  under `~/.claude/projects/`. The project figures cover the folder you started Claude Code
  in. Start it one level higher and that is a different project, with its own separate total.
- **Project history reaches back only as far as your transcripts do.** Claude Code deletes
  session data older than `cleanupPeriodDays`, which is 30 days by default. Once a transcript
  is deleted its cost disappears from the project total as well. Raise that setting in
  `settings.json` if you want a longer history.
- **A few requests are counted twice.** Retries are removed inside a single transcript, but
  resuming a session copies part of the history into a new file. Measured against the
  transcripts this was built on, that came to roughly 1%.
- **A brand-new model may be priced from a guess.** Rates are matched by model id, with a
  built-in fallback for ids the feed has never seen. When that fallback is used, the header
  shows `⚠ unpriced model`. See [Prices](#how-it-works).
- **The price table can lag behind.** It refreshes once a day, and the LiteLLM feed itself
  needs time to pick up a change.
- **The limit percentages are not estimates.** The `5h` and `7d` bars come straight from
  Claude Code. Only the dollar and token figures beside them are calculated here.

This is also why the numbers never match `/cost` exactly. `/cost` reports Claude Code's own
accounting for the current session; this counts every transcript still on disk.

## Commands

| Command | What it does |
| --- | --- |
| `statusline.py` | Renders the line. Reads the session JSON on stdin — this is the call Claude Code makes. |
| `--install [DIR ...]` | Adds the `statusLine` entry to each account's `settings.json`. |
| `--doctor` | Checks the setup: prices, accounts, wiring, and how long a render takes. |
| `--prices` | Prints the rate table currently in use. |
| `--refresh-prices` | Downloads rates now and shows what changed since the last download. |
| `--clear-cache [DIR]` | Deletes one account's scan cache, shared limit readings and refresh lock. |
| `--config-dir=DIR` | Forces a specific account folder instead of detecting one. `--doctor` ignores this and always reports on every account it finds. |
| `--help` | Short usage summary. |

## Configuration

Everything you can tune sits in the `CONFIGURATION` block at the top of the file:

| Setting | Default | Notes |
| --- | --- | --- |
| `LANG` | `"en"` | `"en"` or `"pl"`. Changes the labels only. |
| `LAYOUT` | `"gauges"` | `"gauges"`: four lines, bars aligned in one column. `"compact"`: two lines, bars side by side. |
| `BAR_W` | `10` | Bar width, in characters. |
| `BAR_STYLE` | `"solid"` | `"solid"` paints the bar with a background colour, which stays opaque on a transparent terminal. `"ascii"` draws `█` characters instead. |
| `SHOW_GIT` | `True` | The branch is read straight from `.git/HEAD`; no `git` process is started. |
| `C_*`, `BAR_*` | — | 256-colour numbers. Preview the grey ramp with `for i in $(seq 232 255); do printf "\033[48;5;${i}m %3d \033[0m" $i; done` |

`PRICES_TTL`, `PRICES_WARN_AFTER`, `REFRESH_LOCK_TTL` and `RECENT_DAYS` sit just below the
palette. Leave them alone unless you know why you are changing them.

### The compact layout

```
Opus 5 · max · ai/statusline · ⑂ main · project $5.66 / 5.04M
ctx ████▒▒▒▒▒▒  42% 84k/200k   5h ██▒▒▒▒▒▒▒▒  18%    7d ██████▒▒▒▒  63%    $1.87 session · $367.60 7d
```

## How it works

**Transcripts.** Every assistant message in `~/.claude/projects/*/*.jsonl` carries a `usage`
block. The script adds them up. It skips synthetic models and removes duplicates by
`(message id, requestId)`, so a retried request is not billed twice.

**Prices.** Rates come from the
[LiteLLM feed](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json),
filtered down to Anthropic models. Cache-write and cache-read rates are read per model,
because they are not always a fixed fraction of the input price. A model id is matched exactly
first, then with the date suffix removed, then by the longest matching prefix — so a freshly
released `claude-opus-5-1-20261115` gets `claude-opus-5` rates before the feed catches up. If
nothing matches at all, a built-in family table takes over and the header shows
`⚠ unpriced model`.

The table is refreshed by a detached background process, at most once a day, and at most once
every 15 minutes when a download fails. A render never waits for the network: it uses whatever
is already on disk and starts a download for next time. After four days with no successful
download, the header says so.

**Caching.** Parsed transcripts are cached per file, keyed by modification time and size, and
the whole cache is dropped whenever prices change. A warm render takes a few milliseconds;
`--doctor` prints the real numbers for your machine.

Caches live in `$XDG_CACHE_HOME/cc-statusline`, or `~/.cache/cc-statusline`:

| File | What it holds |
| --- | --- |
| `prices.json` | The rate table, shared by every account. |
| `scan-*.json` | Parsed transcripts — one file per account. |
| `limits-*.json` | Shared limit readings — one file per account. |
| `refresh.lock` | Marks when a download was last attempted. |

`--clear-cache` removes these for one account.

**Multiple accounts.** The account folder is worked out from `transcript_path` in the data
Claude Code sends on stdin, so it is correct whether or not `CLAUDE_CONFIG_DIR` reaches the
subprocess. Several accounts can run side by side: each gets its own scan cache and its own
shared limit readings, and any account other than `~/.claude` is tagged in the header.

## Privacy

The script reads only files that are already on your disk. It makes exactly one outgoing
request, to `raw.githubusercontent.com`, for the price table. Nothing from your sessions or
your code ever leaves the machine.

## Troubleshooting

Start with `--doctor`. It reports which Python is running, how fresh the prices are, every
account it found and whether that account points at *this* file, plus render timings.

**Nothing appears.** Restart Claude Code — `settings.json` is read only at startup.

**Bars are invisible or washed out.** On a transparent terminal, keep `BAR_STYLE = "solid"`.
If the empty part of the bar disappears into your background, raise or lower `BAR_TRACK`. On
a light theme, the grey defaults (`C_SEP`, `C_MUTED`, `BAR_TRACK`) are the first things to change.

**Numbers look wrong after you edited prices or the fallback table.** Run `--clear-cache`.

**`⚠ no price data`.** Run `--refresh-prices` to see the actual network error.

**The numbers do not match `/cost`.** They cannot — see
[Costs and tokens are estimates](#costs-and-tokens-are-estimates).

## Uninstall

1. Remove the `statusLine` key from each `settings.json`. `--install` left a
   `settings.json.bak-<timestamp>` file next to it.
2. Delete the cache folder: `rm -rf ~/.cache/cc-statusline` (or `$XDG_CACHE_HOME/cc-statusline`).
3. Delete `statusline.py`.
4. Restart Claude Code.

## Contributing

Issues and pull requests are welcome. It is a single file — open it and start at the
`CONFIGURATION` block.

## License

[MIT](LICENSE) © Maciej Łotysz
