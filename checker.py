"""
Checker for the Ridgeline weather station network.

Reads a data pack folder (stations.csv, data/Sxx.csv, gps/Sxx.nmea) and writes
flags.csv in the current folder, with one row per data problem found.

Run it like this:
    python checker.py <path to data pack folder>
"""

import sys
import os
import csv
import math
from datetime import datetime, timedelta


# ---------- reading the pack ----------

def read_stations(path):
    stations = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stations[row["station_id"]] = {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "elevation_m": float(row["elevation_m"]),
                "logger_interval_min": int(row["logger_interval_min"]),
            }
    return stations


def read_station_data(path):
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "timestamp_utc": datetime.strptime(row["timestamp_utc"], "%Y-%m-%d %H:%M"),
                "temp_c": float(row["temp_c"]),
                "rh_pct": float(row["rh_pct"]),
                "pressure_hpa": float(row["pressure_hpa"]),
                "wind_kmh": float(row["wind_kmh"]),
                "wind_dir_deg": float(row["wind_dir_deg"]),
                "rain_mm": float(row["rain_mm"]),
                "ghi_wm2": float(row["ghi_wm2"]),
            })
    return rows


def nmea_to_degrees(raw, hemisphere):
    raw = float(raw)
    deg = int(raw / 100)
    minutes = raw - deg * 100
    value = deg + minutes / 60.0
    if hemisphere in ("S", "W"):
        value = -value
    return value


def parse_nmea_file(path):
    # we only use $GPRMC lines: they carry time, date, status and position together
    fixes = []
    with open(path) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 10 or parts[1] != "$GPRMC":
                continue
            status = parts[3]
            if status != "A":
                continue
            logger_ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
            time_str = parts[2]
            date_str = parts[10]
            hh = int(time_str[0:2])
            mm = int(time_str[2:4])
            ss = int(float(time_str[4:]))
            dd = int(date_str[0:2])
            mo = int(date_str[2:4])
            yy = 2000 + int(date_str[4:6])
            gps_ts = datetime(yy, mo, dd, hh, mm, ss)
            lat = nmea_to_degrees(parts[4], parts[5])
            lon = nmea_to_degrees(parts[6], parts[7])
            fixes.append({"logger_ts": logger_ts, "gps_ts": gps_ts, "lat": lat, "lon": lon})
    return fixes


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- flag helper ----------

def fmt(ts):
    return ts.strftime("%Y-%m-%d %H:%M")


def add_flag(flags, station_id, channel, start, end, issue, action, note):
    flags.append({
        "station_id": station_id,
        "channel": channel,
        "start_utc": fmt(start),
        "end_utc": fmt(end),
        "issue": issue,
        "action": action,
        "note": note,
    })


# ---------- checks on the 10-minute data ----------

def check_timestamp_order(rows, station_id, flags):
    # a healthy logger's timestamps always increase. If a timestamp repeats or
    # goes backwards, the logger clock was probably reset (e.g. after a reboot),
    # and we can no longer trust which reading belongs to which time.
    bad_start_idx = None
    max_seen = None
    for i, r in enumerate(rows):
        ts = r["timestamp_utc"]
        if max_seen is not None and ts <= max_seen:
            if bad_start_idx is None:
                bad_start_idx = i
        else:
            if bad_start_idx is not None:
                add_flag(
                    flags, station_id, "all",
                    rows[bad_start_idx]["timestamp_utc"], rows[i - 1]["timestamp_utc"],
                    "duplicate or out-of-order timestamps",
                    "exclude",
                    "logger timestamps repeat or go backwards here, which usually means the "
                    "logger clock reset after a power loss or reboot",
                )
                bad_start_idx = None
        if max_seen is None or ts > max_seen:
            max_seen = ts
    if bad_start_idx is not None:
        add_flag(
            flags, station_id, "all",
            rows[bad_start_idx]["timestamp_utc"], rows[-1]["timestamp_utc"],
            "duplicate or out-of-order timestamps",
            "exclude",
            "logger timestamps repeat or go backwards here, which usually means the "
            "logger clock reset after a power loss or reboot",
        )


def check_missing_gaps(rows, station_id, interval_min, flags):
    expected = timedelta(minutes=interval_min)
    for i in range(1, len(rows)):
        prev_ts = rows[i - 1]["timestamp_utc"]
        cur_ts = rows[i]["timestamp_utc"]
        gap = cur_ts - prev_ts
        if gap > expected * 1.5:
            missing_start = prev_ts + expected
            missing_end = cur_ts - expected
            n_missing = int(gap / expected) - 1
            add_flag(
                flags, station_id, "all", missing_start, missing_end,
                "missing data (" + str(n_missing) + " readings not logged)",
                "exclude",
                "no rows exist for this period, consistent with a connectivity or power outage",
            )


def flag_condition_runs(rows, station_id, channel, condition_fn, issue_text, flags,
                         short_action, long_action, long_threshold, note_fn, merge_gap=0):
    # merge_gap lets a run survive a few good readings in a row without being
    # split into lots of tiny flags, e.g. a saturated humidity sensor that
    # dips just under 100% for a reading or two before pinning again.
    run_start = None
    last_true_i = None
    for i, r in enumerate(rows):
        if condition_fn(r):
            if run_start is None:
                run_start = i
            last_true_i = i
        else:
            if run_start is not None and i - last_true_i > merge_gap:
                _emit_run(rows, station_id, channel, run_start, last_true_i, issue_text, flags,
                          short_action, long_action, long_threshold, note_fn)
                run_start = None
    if run_start is not None:
        _emit_run(rows, station_id, channel, run_start, last_true_i, issue_text, flags,
                   short_action, long_action, long_threshold, note_fn)


def _emit_run(rows, station_id, channel, i0, i1, issue_text, flags,
              short_action, long_action, long_threshold, note_fn):
    n = i1 - i0 + 1
    action = long_action if n >= long_threshold else short_action
    note = note_fn(rows, i0, i1, n)
    add_flag(flags, station_id, channel, rows[i0]["timestamp_utc"], rows[i1]["timestamp_utc"],
              issue_text, action, note)


def check_rh_bounds(rows, station_id, flags):
    def is_bad(r):
        return r["rh_pct"] > 100 or r["rh_pct"] < 0

    def note_fn(rows, i0, i1, n):
        return ("humidity reads above 100%% for " + str(n) + " readings in a row; "
                "a brief overshoot during fog is normal, a long flat run usually means "
                "the sensor is saturated and stuck")

    flag_condition_runs(
        rows, station_id, "rh_pct", is_bad,
        "humidity reading outside the 0-100% physical range",
        flags, short_action="correct", long_action="exclude",
        long_threshold=12,  # 2 hours
        note_fn=note_fn,
        merge_gap=6,  # bridge gaps of up to 1 hour of in-range readings
    )


def check_ghi_bounds(rows, station_id, flags):
    def is_bad(r):
        return r["ghi_wm2"] < 0 or r["ghi_wm2"] > 1361  # solar constant at top of atmosphere

    def note_fn(rows, i0, i1, n):
        return "sunlight reading is negative or above the solar constant (1361 W/m2), which is not physically possible"

    flag_condition_runs(
        rows, station_id, "ghi_wm2", is_bad,
        "sunlight reading outside the physically possible range",
        flags, short_action="exclude", long_action="exclude",
        long_threshold=1,
        note_fn=note_fn,
    )


def check_wind_bounds(rows, station_id, flags):
    def is_bad(r):
        return r["wind_kmh"] < 0 or r["wind_kmh"] > 300 or r["wind_dir_deg"] < 0 or r["wind_dir_deg"] > 360

    def note_fn(rows, i0, i1, n):
        return "wind speed or direction is outside what any station could physically record"

    flag_condition_runs(
        rows, station_id, "wind_kmh", is_bad,
        "wind reading outside the physically possible range",
        flags, short_action="exclude", long_action="exclude",
        long_threshold=1,
        note_fn=note_fn,
    )


def check_rain_bounds(rows, station_id, flags):
    def is_bad(r):
        return r["rain_mm"] < 0

    def note_fn(rows, i0, i1, n):
        return "rain in this 10-minute interval is negative, which is not possible for a tipping bucket gauge"

    flag_condition_runs(
        rows, station_id, "rain_mm", is_bad,
        "negative rain reading",
        flags, short_action="exclude", long_action="exclude",
        long_threshold=1,
        note_fn=note_fn,
    )


def check_pressure_bounds(rows, station_id, flags):
    def is_bad(r):
        return r["pressure_hpa"] < 300 or r["pressure_hpa"] > 1100

    def note_fn(rows, i0, i1, n):
        return "station pressure is outside the range seen anywhere on Earth, even at extreme elevation"

    flag_condition_runs(
        rows, station_id, "pressure_hpa", is_bad,
        "pressure reading outside the physically possible range",
        flags, short_action="exclude", long_action="exclude",
        long_threshold=1,
        note_fn=note_fn,
    )


def check_stuck_values(rows, station_id, flags):
    # a sensor that reports the exact same number for hours on end, with no noise
    # at all, is almost certainly frozen or disconnected rather than reporting
    # real, unchanging weather. We ignore ghi_wm2==0 and rain_mm==0 because those
    # are genuinely flat at night / during dry spells.
    stuck_threshold = 18  # 18 x 10 minutes = 3 hours
    channels = ["temp_c", "rh_pct", "pressure_hpa", "wind_kmh", "ghi_wm2"]
    for channel in channels:
        run_start = 0
        for i in range(1, len(rows) + 1):
            same_as_prev = i < len(rows) and rows[i][channel] == rows[i - 1][channel]
            if not same_as_prev:
                run_len = i - run_start
                if run_len >= stuck_threshold:
                    value = rows[run_start][channel]
                    skip = (channel == "ghi_wm2" and value == 0) or (channel == "rain_mm" and value == 0)
                    if not skip:
                        add_flag(
                            flags, station_id, channel,
                            rows[run_start]["timestamp_utc"], rows[i - 1]["timestamp_utc"],
                            "value stuck at " + str(value) + " for " + str(run_len) + " readings in a row",
                            "exclude",
                            "reading does not move at all for over " + str(run_len * 10) +
                            " minutes; real weather has at least some noise, this looks like a frozen or disconnected sensor",
                        )
                run_start = i


def check_spikes(rows, station_id, flags):
    # compare each reading to the typical value in the hour around it. A single
    # reading that jumps far away and (usually) comes straight back next step is
    # a glitch, not real weather, for slow-moving variables like temperature and
    # pressure. Neighbours are picked by how close they are in TIME, not just by
    # row position, so a reading right next to a missing-data gap is compared
    # only to real nearby readings, not to rows from a day later.
    max_gap = timedelta(minutes=70)
    window = 6  # +/- 6 readings = +/- 1 hour, when nothing is missing nearby
    limits = {"temp_c": 15.0, "pressure_hpa": 5.0}
    for channel, limit in limits.items():
        for i in range(len(rows)):
            ts = rows[i]["timestamp_utc"]
            lo = max(0, i - window)
            hi = min(len(rows), i + window + 1)
            neighbours = [
                rows[j][channel] for j in range(lo, hi)
                if j != i and abs((rows[j]["timestamp_utc"] - ts)) <= max_gap
            ]
            if not neighbours:
                continue
            neighbours.sort()
            local_median = neighbours[len(neighbours) // 2]
            diff = rows[i][channel] - local_median
            if abs(diff) > limit:
                ts = rows[i]["timestamp_utc"]
                add_flag(
                    flags, station_id, channel, ts, ts,
                    "single reading is " + str(round(abs(diff), 1)) + " away from the hour around it",
                    "exclude",
                    "value " + str(rows[i][channel]) + " does not fit the smooth trend just before "
                    "and after it (local median " + str(round(local_median, 1)) + "); looks like a sensor glitch",
                )


def build_network_baseline(all_rows_by_station, channel):
    # Demean each station's channel (subtract its own six-week average) so
    # stations that naturally read differently (e.g. different elevation, or a
    # sheltered vs exposed site) can be compared on the same scale, then average
    # those anomalies across stations at each timestamp. This gives the shared
    # regional weather signal, which a real front or calm spell moves together
    # at every station, unlike a single station's own instrument fault.
    station_means = {}
    for station_id, rows in all_rows_by_station.items():
        values = [r[channel] for r in rows]
        station_means[station_id] = sum(values) / len(values)

    anomalies_by_timestamp = {}
    for station_id, rows in all_rows_by_station.items():
        mean = station_means[station_id]
        for r in rows:
            ts = r["timestamp_utc"]
            anomaly = r[channel] - mean
            anomalies_by_timestamp.setdefault(ts, []).append(anomaly)

    network_avg = {}
    for ts, values in anomalies_by_timestamp.items():
        network_avg[ts] = sum(values) / len(values)

    return station_means, network_avg


def check_channel_step_change(rows, station_id, channel, unit, phrase, station_mean,
                               network_avg, raw_threshold, half_window, flags):
    # Look for a sudden, lasting jump in a channel that is specific to this
    # station: subtract the regional average first, so a change that affects
    # every station together (a front, a calm spell, a gusty afternoon)
    # cancels out, and only a jump unique to this one station's sensor is
    # left. Compare the window just before each point to the window just
    # after it, and only flag a jump that stands out against the station's
    # own typical change over a window that size -- that pattern looks like
    # an instrument change, not weather.
    residual = [
        (r[channel] - station_mean) - network_avg.get(r["timestamp_utc"], 0.0)
        for r in rows
    ]

    all_jumps = []
    for i in range(half_window, len(rows) - half_window):
        before = residual[i - half_window:i]
        after = residual[i:i + half_window]
        before_mean = sum(before) / len(before)
        after_mean = sum(after) / len(after)
        all_jumps.append((after_mean - before_mean, i))

    if not all_jumps:
        return

    sizes = sorted(abs(d) for d, i in all_jumps)
    median_size = sizes[len(sizes) // 2]
    best_diff, best_i = max(all_jumps, key=lambda x: abs(x[0]))

    ts = rows[best_i]["timestamp_utc"]
    raw_before = sum(r[channel] for r in rows[best_i - half_window:best_i]) / half_window
    raw_after = sum(r[channel] for r in rows[best_i:best_i + half_window]) / half_window

    # require the jump to be large both after removing the regional weather
    # signal, AND in the station's own raw readings -- this keeps out tiny
    # jumps that only look big because the regional signal is unusually flat
    is_large_enough = abs(best_diff) > raw_threshold and abs(raw_after - raw_before) > raw_threshold
    is_an_outlier = abs(best_diff) > 6 * max(median_size, 0.1)
    if is_large_enough and is_an_outlier:
        span_desc = str(round(half_window * 10 / 60.0, 1)) + " hours"
        add_flag(
            flags, station_id, channel, ts, rows[-1]["timestamp_utc"],
            phrase + " shifts by about " + str(round(raw_after - raw_before, 1)) +
            " " + unit + " over about " + span_desc + ", relative to the rest of the network, and stays shifted",
            "keep",
            "other stations do not show this change at the same time, so it is not shared weather; "
            "the station's own reading goes from about " + str(round(raw_before, 1)) +
            " to " + str(round(raw_after, 1)) + " " + unit + " over " + span_desc + " and never comes back, "
            "which looks like an instrument change; readings after this point may not be comparable "
            "to the readings before it",
        )


# ---------- checks using the GPS files ----------

def check_gps_location(fixes, station_id, install_lat, install_lon, flags):
    if not fixes:
        return
    lats = sorted(f["lat"] for f in fixes)
    lons = sorted(f["lon"] for f in fixes)
    median_lat = lats[len(lats) // 2]
    median_lon = lons[len(lons) // 2]
    dist_km = haversine_km(median_lat, median_lon, install_lat, install_lon)
    if dist_km > 1.0:
        add_flag(
            flags, station_id, "metadata",
            fixes[0]["logger_ts"], fixes[-1]["logger_ts"],
            "GPS position is about " + str(round(dist_km, 1)) + " km from the stations.csv location",
            "correct",
            "GPS fixes cluster tightly around lat " + str(round(median_lat, 4)) + ", lon " +
            str(round(median_lon, 4)) + " for all six weeks, while stations.csv lists " +
            str(install_lat) + ", " + str(install_lon) +
            "; this looks like a typo on the install sheet rather than a station that moved",
        )


def check_gps_clock(fixes, station_id, flags):
    skew_limit = timedelta(minutes=5)
    run_start = None
    for i, fix in enumerate(fixes):
        diff = abs(fix["logger_ts"] - fix["gps_ts"])
        if diff > skew_limit:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                _emit_gps_skew(fixes, station_id, run_start, i - 1, flags)
                run_start = None
    if run_start is not None:
        _emit_gps_skew(fixes, station_id, run_start, len(fixes) - 1, flags)


def _emit_gps_skew(fixes, station_id, i0, i1, flags):
    example = fixes[i0]
    minutes_off = (example["logger_ts"] - example["gps_ts"]).total_seconds() / 60.0
    direction = "behind" if minutes_off < 0 else "ahead of"
    add_flag(
        flags, station_id, "metadata",
        fixes[i0]["logger_ts"], fixes[i1]["logger_ts"],
        "logger clock is about " + str(round(abs(minutes_off), 1)) + " minutes " + direction + " GPS time",
        "exclude",
        "GPS gives the true UTC time independent of the logger clock; during this window the logger "
        "timestamp does not match it, so any reading timestamps here are unreliable",
    )


# ---------- main ----------

def run_checks_for_station(station_id, station_info, rows, pack_dir,
                            pressure_mean, pressure_network_avg, flags):
    check_timestamp_order(rows, station_id, flags)
    check_missing_gaps(rows, station_id, station_info["logger_interval_min"], flags)
    check_rh_bounds(rows, station_id, flags)
    check_ghi_bounds(rows, station_id, flags)
    check_wind_bounds(rows, station_id, flags)
    check_rain_bounds(rows, station_id, flags)
    check_pressure_bounds(rows, station_id, flags)
    check_stuck_values(rows, station_id, flags)
    check_spikes(rows, station_id, flags)
    check_channel_step_change(rows, station_id, "pressure_hpa", "hPa", "pressure",
                               pressure_mean, pressure_network_avg, 3.0, 6, flags)
    # a similar check for wind_kmh was tried and dropped: a short window
    # (1 hour) flags normal gusts on almost every station, and a long window
    # (3 days) is too insensitive to catch a real instrument change against
    # how much wind naturally varies station to station. See DECISIONS.md.

    gps_path = os.path.join(pack_dir, "gps", station_id + ".nmea")
    if os.path.exists(gps_path):
        fixes = parse_nmea_file(gps_path)
        check_gps_location(fixes, station_id, station_info["lat"], station_info["lon"], flags)
        check_gps_clock(fixes, station_id, flags)


def write_flags_csv(flags, out_path):
    fieldnames = ["station_id", "channel", "start_utc", "end_utc", "issue", "action", "note"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in flags:
            writer.writerow(row)


def main():
    if len(sys.argv) < 2:
        print("usage: python checker.py <path to data pack folder>")
        sys.exit(1)

    pack_dir = sys.argv[1]
    stations = read_stations(os.path.join(pack_dir, "stations.csv"))

    all_rows_by_station = {}
    for station_id in sorted(stations.keys()):
        data_path = os.path.join(pack_dir, "data", station_id + ".csv")
        if os.path.exists(data_path):
            all_rows_by_station[station_id] = read_station_data(data_path)

    pressure_means, pressure_network_avg = build_network_baseline(all_rows_by_station, "pressure_hpa")

    flags = []
    for station_id in sorted(all_rows_by_station.keys()):
        run_checks_for_station(
            station_id, stations[station_id], all_rows_by_station[station_id], pack_dir,
            pressure_means[station_id], pressure_network_avg, flags,
        )

    # a stable sort makes the output easy to read and easy to diff between runs
    flags.sort(key=lambda r: (r["station_id"], r["start_utc"], r["channel"]))

    out_path = os.path.join(os.getcwd(), "flags.csv")
    write_flags_csv(flags, out_path)
    print("wrote " + str(len(flags)) + " flags to " + out_path)


if __name__ == "__main__":
    main()
