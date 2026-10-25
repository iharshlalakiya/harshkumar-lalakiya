# Decisions

## 1. The visit

I'd send the technician to **S03 (Valley Library)**, and have them open the enclosure and check
the temperature sensor's connector and cable before anything else.

S03's temperature channel spikes to exactly &minus;40.0 °C or 85.0 °C four times, roughly a week
apart over six weeks, and froze dead flat at 6.5 °C for 80 straight hours (2025-03-18 to
2025-03-21) instead of faulting cleanly. That's the signature of a loose or corroding
connection, not a one-off glitch: intermittent, recurring, sometimes a clean error code and
sometimes a stuck value. It only hits temperature — the other channels on the same logger are
fine — which rules out the logger board and points at the sensor or its lead.

Second choice was S02: pressure jumps ~7 hPa in under an hour on 2025-03-21, exactly when the log
says someone swapped the pressure board without calibrating it. I didn't send them there because
that fault is already fully diagnosed, costs only one channel, and fixing it is a ten-minute job
with a reference barometer whenever someone gets to it. S03's fault is still active, costs an
entire channel for days at a time, and its cause isn't known yet — that's the one that needs an
hour on site this week.

## 2. My least certain call

**S01, pressure_hpa, 2025-04-04 to 2025-04-11, "keep"** — pressure at S01 shifts ~6 hPa within an
hour on 2025-04-04 and stays shifted, and no other station shows a matching move that day.

Every other step-change I flagged (S02) lines up with a maintenance-log entry. S01 has nothing
logged near that date. S01 is also by far the highest station (2913 m; the next highest is
~1900 m), so a short-lived downslope wind or inversion event there wouldn't necessarily show up
at the lower stations — the data alone can't tell a real local weather event apart from a sensor
fault. What would settle it: a maintenance-log entry for S01 around that date (even "nothing to
report" narrows it down), or a second high-elevation reference for that day. Without either, I
left it as "keep" rather than "exclude" — better to not throw away real high-altitude weather
just because it looks odd next to the valley stations.

## 3. Where the AI was wrong

**NMEA field index.** I wrote the GPS date parser assuming the date field was `parts[9]`, from a
quick read of the sentence structure rather than counting fields on an actual line. It ran clean
on the first several files, then crashed with `invalid literal for int(): '0.'` partway through a
batch. Printing an actual split line with indices showed the date was really at `parts[10]` — the
logger-timestamp column in front of `$GPRMC` shifts every field over by one. The scarier part is
it wouldn't always have crashed; it would have silently read the wrong field on most lines.

**Pressure step-change false positives.** My first version compared the average pressure in the
6 hours before each point to the 6 hours after, and flagged each station's single biggest jump.
It nailed S02 — but run across all 8 stations, nearly every one got a near-identical "shifts by
~9–14 hPa" flag, all clustered in the same few early-April days. That pattern was the tell: a
shared regional pressure trend was being mistaken for a station-specific fault, because a wide
enough window always contains some point with the steepest local slope, trend or not. I only
caught it by reading the actual output, not by trusting the check ran without errors. Fix:
detrend each station against the network average first, so a shared front cancels out and only a
jump unique to one station survives.

## 4. Next year

**Keep as is:** physical-bounds checks, duplicate/out-of-order timestamps, missing-data gaps,
GPS clock-skew — cheap, near-zero false positives, no per-station tuning needed.

**Change:** the network-average detrending only works if "the network" shares one weather regime.
At 500 stations I'd cluster by distance/elevation first, so a station isn't compared against
others hundreds of kilometres away in a different climate.

**Drop:** the fixed spike thresholds (15 °C, 5 hPa) came from eyeballing one network's
late-winter data. At scale, across seasons and climates, I'd compute each station's threshold
from its own recent variability instead of one global constant.

**Refuse to automate:** silently applying the `correct` action. At 500 stations nobody reviews
every row, so an automatic correction becomes ground truth by default with no human in the loop.
I'd restrict the checker to `exclude`/`keep`, write candidate corrections to a separate review
file, and only apply them after a human batch-approves them — including metadata fixes like the
S05 location typo, since GPS itself can be wrong and a station's address shouldn't be rewritten
automatically.

## 5. By hand

S04 is at longitude **105.146° W**. Ignoring the small equation-of-time wobble, solar noon is
12:00 local solar time, converted to UTC by the longitude offset (15° of longitude = 1 hour):

```text
105.146 / 15 ≈ 7.01 hours west of Greenwich
solar noon (UTC) ≈ 12:00 + 7.01 h ≈ 19:00 UTC
```

The equation of time shifts this by another 7–8 minutes in late March, well inside "roughly" —
so about **19:00 UTC** on 2025-03-20.

Useful here as a sanity check: `ghi_wm2` should peak close to this time on a clear day at S04. If
a station's measured sunlight peak drifted noticeably from its calculated solar noon, that's a
sign its clock is wrong even without a GPS file to check against directly — the same kind of
problem I found at S06 by comparing logger time to GPS time.
