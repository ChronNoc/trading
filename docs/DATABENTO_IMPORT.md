# Databento: buying and importing MNQ history

Instead of recording sessions live for hours, you can buy months of past MNQ order-book data from
Databento and use it at once: every day becomes a recorded session you can replay, research and
paper-test on. **Historical only**: the live feed stays Bookmap.

## What you need

* A Databento account (databento.com). New accounts have come with free credit; check the current
  offer on their site. Pay as you go is enough; no subscription plan is needed for history.
* Your API key: in the Databento portal, **API Keys**. It starts with `db-`.

## Buy and import from the app

1. Double-click **Databento Import** on the desktop (or run
   `.venv\Scripts\pythonw.exe -m tools.databento_import_app` in the project folder).
2. In **Download from Databento**, press **Set key…** and paste your key. It is saved in your Windows
   profile (`%APPDATA%\MNQ Intelligence\databento_api_key.txt`), never in the project folder, and only
   its last four characters are ever shown.
3. Choose the **History** (the last 1, 5, 20, 60, 120 or 250 trading days, up to yesterday) and the
   **Hours**:
   * **Trading hours only, 09:30–16:50 New York** (the default): every weekday from the opening bell to
     just after Lucid's 16:45 auto-close. It covers everything the paper account trades (the governor
     trades 09:35–16:15 and is flat by 16:40) and costs far less than whole days. The overnight high
     and low are then unknown (the data starts at the bell, so no half-night "overnight" level is
     invented); setups that need them are skipped on those days.
   * **Whole day, 24 hours**: the full CME session. Much more expensive: on 2026-11-09 Databento quoted
     $68.55 for one calendar week of whole days, about $13 per trading day.
4. Press **Check price**. Databento quotes exactly the days you do not have yet. Nothing is bought.
5. Press **Buy and import ($…)** and confirm the exact price. The app buys the data, waits while
   Databento prepares it, downloads one file per day, and imports it. Progress shows in the
   **Progress** card; **Stop** stops at the next safe point.

Need a specific range instead? Use the command line (below).

## The money rules

* **Nothing is bought without your confirmation of the exact price**, and a purchase never spends more
  than you confirmed: the price is checked again just before buying, and nothing is bought if it rose.
* **No day is paid for twice.** Days already imported are left out of the price. Every day bought is
  written to `data/.databento_downloads/jobs.json` the moment it is bought, so a run that was stopped,
  timed out or crashed continues from that purchase instead of buying again. Only if Databento has
  expired that purchase (it keeps prepared files for a limited time) is the day priced again, and you
  confirm again.
* **A slow or silent Databento does not end a paid run.** Calls that buy nothing (the price, a job's
  progress, its file list, a download) are tried again for about 8 minutes, and a job being prepared is
  waited out through up to 20 unanswered checks in a row. A purchase is never sent twice: if its reply
  is lost, the app asks Databento whether it went through and continues with that job; if Databento
  shows no such job, the run stops and says to look under **Download center** in the Databento portal
  before buying that day again.
* Charges go to your Databento account, free credit first.

## How big and how long (first real day, 2026-11-02, trading hours)

| | |
|---|---|
| Price | about $8.80 a day ($44.02 for 5 trading days) |
| Download | 1.5 GB per day (`.csv.zst`), deleted once imported |
| Rows | 44.8 million book updates, 1.34 million trades |
| Import | about 4 minutes per day |
| Kept on disk | about 450 MB per day |

Each day is imported as soon as its file is downloaded, and the file is deleted once the day is in,
before the next one is fetched. A long range therefore needs free space for one day's download at a
time (1.5–2.7 GB) plus about 450 MB per day kept, and a run that stops part-way has already imported
every day it downloaded.

## What exactly is bought

| Setting | Value |
|---|---|
| Dataset | `GLBX.MDP3` (CME Globex) |
| Schema | `mbp-10`: the top 10 price levels of the book, with every trade (no separate trades file needed) |
| Symbol | `MNQ.v.0`: each day, the MNQ contract with the most volume (one contract a day) |
| Format | CSV, zstd-compressed, one file per UTC day, with the contract symbol |
| Hours | trading hours only: 09:30–16:50 New York of each weekday, one purchase per day (a Databento batch has one start and one end); whole day: whole UTC days |

A day imported with trading hours only counts as imported: buying the same day again as a whole day
is skipped. Choose the hours before buying a range.

Imported order books have 10 levels; live Bookmap recordings have up to 20. Every book feature the
strategy and machine learning use reads only the top 10 levels of each side, live and imported alike
(since 2026-11-09), so a day imported from Databento and a day recorded live are measured the same way.

## Command line

```
py -m tools.databento_download --days 5                        # price only: buys nothing
py -m tools.databento_download --start 2026-08-01 --end 2026-08-31 --hours full
py -m tools.databento_download --days 20 --buy --max-cost 40   # buys only if it costs $40 or less
```

`--days` counts trading days back from yesterday; `--hours rth` (the default) or `--hours full`.

The key comes from `DATABENTO_API_KEY` or the one saved from the window. `--max-cost` is your
confirmation; without `--buy` nothing is ever bought. `--allow-training` lets machine learning train on
the imported days.

## Files you download yourself

You can still buy on databento.com and drop the files on the window (or on the desktop shortcut). Use:
GLBX.MDP3, `mbp-10`, one contract such as `MNQ.v.0`, **CSV** encoding (zstd or gzip compression is
fine), split by day. Databento's binary DBN format is refused, with a message asking for CSV.

## Where things go

* Sessions: `data/raw/<day>/session_<day>T000000Z__databento/`. They appear on the **Sessions and
  Replay** page of the main app within about half a minute. Importing the same day twice is skipped.
* Downloads wait in `data/.databento_downloads/` and are deleted once imported; files that could not be
  imported are kept so a retry does not need to buy them again. Both folders are gitignored.
* Imported days count for replay and research when the importer's quality checks pass. Machine-learning
  training stays **off** unless you tick *Let machine learning train on this data* before importing or
  buying (or pass `--allow-training`). For days already imported:

  ```
  py -m tools.databento_training          # which imported days may train, and why not
  py -m tools.databento_training --on     # let machine learning train on them (--off to undo)
  ```

  The data is never changed, and a day whose quality checks failed stays out of training. A day may
  train when at most 1 trade in 10,000 had no buyer/seller side (Databento marks a few a day; they are
  left out); more than that keeps it replay-only.
* After an import the walk-forward volatility history is updated, so imported first hours count
  towards the 30 days its labels need.

## Safety

* `app/importers/databento_api.py` is the only module allowed to use the network (a user-approved
  exception recorded in `AGENTS.md`). It never trades and never talks to a broker.
* The key is sent only to `*.databento.com` over HTTPS. A download link that redirects anywhere else
  does not receive it.
