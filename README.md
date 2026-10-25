Harshkumar Lalakiya
harshlalakiya.me@gmail.com
Video: TODO — add the unlisted YouTube/Drive link here before submitting

# Ridgeline Weather Network — data health check

Six weeks of 10-minute data from 8 volunteer-run weather stations, checked for problems that
would affect the coordinator's spring climate summary and this week's technician visit.

## What's here

| Path | What it is |
|---|---|
| `checker.py` | Reads a data pack folder and writes `flags.csv` |
| `flags.csv` | Output of `checker.py` on this pack — one row per problem found |
| `report.html` | Two-page report for the coordinator, answering the two questions in the brief |
| `DECISIONS.md` | The five written-answer questions |
| `ai_logs/` | Raw exports of the AI chats used while building this |
| `stations.csv`, `data/`, `gps/`, `maintenance_log.md` | The data pack itself — **not committed**, see `.gitignore` |

## How to run it

```text
python checker.py 
```

The pack folder must contain `stations.csv`, a `data/` folder of `Sxx.csv` files, and a `gps/`
folder of `Sxx.nmea` files, in the same layout as the pack this was built against. The script
writes `flags.csv` into whatever folder you run it from, not into the pack folder. Only the
Python standard library is used, so any Python 3 works — a `venv/` is included for convenience
but isn't required.

```text
python -m venv venv
venv\Scripts\python checker.py path\to\pack
```

## What each check does

- **Timestamp order** — flags any stretch where a logger's timestamps repeat or go backwards
  (seen at S06 and S08, both after a reboot).
- **Missing data** — flags gaps longer than 1.5× the logging interval.
- **Physical-range checks** — humidity outside 0–100%, sunlight outside 0–1361 W/m² (the solar
  constant), wind/direction/rain/pressure outside what any real station could record. Short
  overshoots are marked `correct` (clip to the limit); long stuck-at-the-limit runs are marked
  `exclude`.
- **Stuck values** — any channel that reports the exact same number for 3+ hours straight
  (except sunlight and rain sitting at a legitimate zero), which looks like a frozen or
  disconnected sensor rather than real weather.
- **Spikes** — a single reading that jumps far from the hour around it and (usually) comes
  straight back; only applied to temperature and pressure, the two slow-moving channels where a
  real value can't plausibly jump and revert within 10 minutes. Neighbours are matched by time,
  not row position, so a gap in the data doesn't get bridged into a false spike.
- **Pressure step-change** — a sustained jump in pressure, specific to one station: each
  station's pressure is first compared against the network's shared average (so a real weather
  front, which moves every station together, cancels out), then checked for an abrupt,
  one-hour-scale, lasting shift. This is what catches an uncalibrated sensor swap.
- **GPS position check** — compares each station's own logged GPS fixes against the coordinates
  in `stations.csv`; flags a consistent mismatch as a likely install-sheet typo rather than a
  moved station.
- **GPS clock check** — compares the logger's own timestamp against the GPS sentence's own UTC
  time; flags any stretch where they disagree, which means the logger clock (not the weather
  data) can't be trusted there.

## What I didn't get to

- A similar step-change check for `wind_kmh`, to catch the anemometer swap at S07 automatically.
  I tried it and dropped it — see `DECISIONS.md`, question 3, for why — and documented that
  finding by hand in `report.html` instead of in `flags.csv`.
- The checker doesn't cross-check `wind_dir_deg` for physical plausibility beyond the 0–360°
  range (e.g. a direction that flips 180° between adjacent readings with no calm in between).
- No automated check for the radiation-shield/spider-web type notes in `maintenance_log.md` —
  those aren't visible in the numbers, so a human still has to read the log.
