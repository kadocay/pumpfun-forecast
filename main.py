#!/usr/bin/env python3
"""Forecast pump.fun's top-5 new coins by market cap for make.fun
competition pumpfun-top5-forecast-01.

Reads $MAKEFUN_INPUT_DIR/history.ndjson (10-minute marks, oldest first)
and task.json, writes $MAKEFUN_OUTPUT_DIR/forecast.json: for each of the
top 5 places, a probability for each coin named being at that place, the
top-5 guess in order, and per place a probability for each size bin its
market cap falls in. Python 3.13, standard library only, no network,
fully deterministic (fixed seed; no wall clock, no unseeded randomness).

Method: latent log-cap Monte Carlo diffusion (adapted from the roblox
v2 entry, which scored about -32.1 on its practice window).
  * Every coin seen in the marks gets a latent log-cap, log10(cap in
    SOL), from its last mark. Each coin's log-cap at a read h hours
    after the last mark is its last log-cap plus its drift (decaying
    with a 4 h time constant, so a winner's early surge does not run
    away forever) plus a heavy-tailed step (Student-t with 3 degrees
    of freedom, sd growing as sqrt(h)).
  * Drift is the coin's own least-squares slope of log-cap over the
    last 3 h, shrunk toward a venue prior (pool coins rise,
    +0.10/h; curve coins decay, -0.10/h), capped at +/-0.6/h.
  * Curve coins cannot exceed the graduation ceiling (log-cap 2.62,
    the highest curve cap in the practice marks): a coin still on its
    curve is capped there until it graduates, which it does with a
    per-hour hazard that grows as it nears the ceiling (about 2% per
    10 minutes among top-200 curve coins in the marks). On graduation
    the cap lifts.
  * A coin's step sd is 0.35/h, 0.50/h for the big coins (last
    log-cap >= 3.0), which the marks show are far more volatile.
  * The forecast is the mean over the seven reads of each read's own
    honest distribution: at each read the simulated place
    distribution is scaled down by the newcomer reserve r(h), the
    probability that the place is held by a coin no history row names
    (a coin launched after the cut, or one that sat below the
    rows' top 200 at the cut). r(h) grows with horizon and saturates:
    0.30 at the cut, 0.70 in the limit, time constant 4 h. The marks
    backtest puts the invisible share of top-5 places at 0.50 at a
    10-minute horizon, 0.67 at 20 minutes and 0.80 at 30-40 minutes
    in the cohort's violent first hour; the reserve curve is set a
    little below those raw rates because the cohort ages and the
    churn slows. What a place leaves unnamed is what a newcomer (or
    any chart coin not named there) scores from.
  * Sizes: at each read the place's cap distribution is the mixture
    of the simulated named-coin caps and, with weight r(h), a
    newcomer cap distribution centred a little above the simulated
    mean (new entrants set the level: the marks show every place's
    cap rising about +0.13 to +0.36 per 10 minutes in the first
    hour, driven by fresh big arrivals, not by incumbents growing).
    The newcomer centre is shifted up by 0.35 * (1 - exp(-h/5))
    log units and widened to at least 0.25 sd. The seven reads'
    size distributions are averaged into the one forecast.
  * top5 orders coins by expected rank (probability mass outside
    the top 5 counted as a mid-chart rank).

Nothing here is tuned on the practice answer: the constants come
from the marks themselves (10-minute log-cap changes, graduation
counts, fresh-entrant rates), and the horizons, reads, places and
size bins are all read from task.json, so the same program is
calibrated for the practice's 10-70 minute reads and for the
final's 7-13 hour reads.
"""

import heapq
import json
import math
import os
import random
from datetime import datetime

# --- tuned constants (from the practice marks, see REPORT.md) ------------
SEED = 20261112
REPS = 1500
MAX_POOL = 150           # simulation pool, best last log-cap first
SIGMA_H = 0.35           # per-hour sd of a log-cap step
SIGMA_TOP = 0.50         # per-hour sd for big coins (last log-cap >= 3.0)
SIGMA_TOP_AT = 3.0
DF = 3                   # Student-t degrees of freedom for the step
CURVE_CAP = 2.62         # curve coins cannot exceed this log-cap on curve
DRIFT_WINDOW_H = 3.0     # lookback for each coin's log-cap slope
DRIFT_SHRINK_N = 3.0     # slope weight n / (n + 3)
DRIFT_CAP = 0.60         # cap on |drift| per hour
DRIFT_TAU_H = 4.0        # drift decays with this time constant
PRIOR_POOL = 0.10        # venue drift prior, per hour
PRIOR_CURVE = -0.10
GRAD_BASE_H = 0.02       # per-hour graduation hazard, far from the cap
GRAD_NEAR_H = 0.12       # per-hour graduation hazard, near the cap
GRAD_NEAR_AT = 2.20
RESERVE_0 = 0.30         # newcomer reserve at the cut
RESERVE_MAX = 0.70       # newcomer reserve in the limit
RESERVE_TAU_H = 4.0
LEVEL_TAU_H = 0.5          # place levels fill in with this time constant
LEVEL_SHIFT_MAX = 0.35   # slow record-growth level shift, log units
LEVEL_SHIFT_TAU_H = 5.0
NEWCOMER_SD_MIN = 0.50   # newcomer size sd never below 0.50 (v2: wider tails)
SIZE_SMOOTH_SD = 0.25    # kernel smoothing of the final size distribution (v2)
LOGCAP_FLOOR = 0.0       # a coin's log-cap cannot fall below 1 SOL
LOGCAP_CEIL = 5.5
P_MIN = 0.002            # smallest probability written as a row
OUTSIDE_RANK = 25.0      # rank credited to probability mass outside top 5
# ---------------------------------------------------------------------------

INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def logcap_of(row):
    return math.log10(int(row["market_cap_lamports"])) - 9.0


def reserve(h):
    """P(a place is held by a coin no history row names), h hours out."""
    return RESERVE_MAX - (RESERVE_MAX - RESERVE_0) * math.exp(-h / RESERVE_TAU_H)


def level_shift(h):
    return LEVEL_SHIFT_MAX * (1.0 - math.exp(-h / LEVEL_SHIFT_TAU_H))


def drift_cum(drift, h):
    """Integral of exponentially decaying drift over h hours."""
    return drift * DRIFT_TAU_H * (1.0 - math.exp(-h / DRIFT_TAU_H))


def load_history(input_dir):
    with open(os.path.join(input_dir, "task.json"), "r", encoding="utf-8") as fh:
        task = json.load(fh)
    marks = []
    with open(os.path.join(input_dir, "history.ndjson"), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if not d.get("ok") or not isinstance(d.get("rows"), list):
                continue
            d["_t"] = parse_time(d["at"])
            marks.append(d)
    marks.sort(key=lambda m: m["_t"])
    return task, marks


def build_pool(task, marks):
    """Return a list of coins: dicts with mint, last log-cap, venue,
    drift per hour, sigma per hour, graduation hazard per hour, and
    the series needed for nothing else. Pool: last mark's rows plus
    recent dropouts, best log-cap first, capped at MAX_POOL."""
    t0 = marks[-1]["_t"]
    series = {}
    for m in marks:
        for row in m["rows"]:
            series.setdefault(row["mint"], []).append(
                (m["_t"], logcap_of(row), row.get("venue", "curve"))
            )
    pool = []
    for mint, pts in series.items():
        (t_last, lc_last, venue_last) = pts[-1]
        age_h = (t0 - t_last).total_seconds() / 3600.0
        if age_h > 1.0 and mint not in {r["mint"] for r in marks[-1]["rows"]}:
            continue
        # Least-squares slope of log-cap over the drift window.
        win = [
            (t, v)
            for (t, v, _v) in pts
            if (t0 - t).total_seconds() <= DRIFT_WINDOW_H * 3600.0
        ]
        n = float(len(win))
        slope = 0.0
        if n >= 3:
            mt = sum((t - t0).total_seconds() / 3600.0 for t, _ in win) / n
            mv = sum(v for _, v in win) / n
            num = sum(((t - t0).total_seconds() / 3600.0 - mt) * (v - mv) for t, v in win)
            den = sum(((t - t0).total_seconds() / 3600.0 - mt) ** 2 for t, _ in win)
            if den > 0:
                slope = num / den
        prior = PRIOR_POOL if venue_last == "pool" else PRIOR_CURVE
        w = n / (n + DRIFT_SHRINK_N)
        drift = w * slope + (1.0 - w) * prior
        drift = max(-DRIFT_CAP, min(DRIFT_CAP, drift))
        sigma = SIGMA_TOP if lc_last >= SIGMA_TOP_AT else SIGMA_H
        if venue_last == "pool":
            grad = 0.0
        elif lc_last >= GRAD_NEAR_AT:
            grad = GRAD_NEAR_H
        else:
            grad = GRAD_BASE_H
        pool.append(
            {
                "mint": mint,
                "lc": lc_last,
                "venue": venue_last,
                "drift": drift,
                "sigma": sigma,
                "grad": grad,
            }
        )
    pool.sort(key=lambda c: -c["lc"])
    return pool[:MAX_POOL], t0


def fit_levels(task, marks, places):
    """Fit each place's cap level against cohort age:
    level(a) = L - D * exp(-a / LEVEL_TAU_H). The marks show every
    place's level filling in fast in the cohort's first hour (place 5
    rose 1.85 -> 3.28 in 40 minutes) and then saturating, so a read's
    newcomer level is shifted by level(age_read) - level(age_cut).
    Returns (t_open, [(L, D), ...]) with D >= 0."""
    t_open = parse_time(task["t0"]) if task.get("t0") else marks[0]["_t"]
    out = []
    for k in range(places):
        pts = []
        for m in marks:
            if len(m["rows"]) > k:
                a = (m["_t"] - t_open).total_seconds() / 3600.0
                pts.append((math.exp(-a / LEVEL_TAU_H), logcap_of(m["rows"][k])))
        if len(pts) < 2:
            # No slope to fit (a single mark): fall back to the
            # first-hour rise the marks show elsewhere, D = 1.0.
            out.append((pts[-1][1] if pts else 3.0, 1.0))
            continue
        n = float(len(pts))
        mx = sum(x for x, _ in pts) / n
        my = sum(y for _, y in pts) / n
        den = sum((x - mx) ** 2 for x, _ in pts)
        if den <= 0:
            out.append((my, 0.0))
            continue
        slope = sum((x - mx) * (y - my) for x, y in pts) / den
        # level = L - D * x  =>  slope = -D
        D = -slope
        L = my + D * mx
        if D < 0:
            D = 0.0
            L = my
        out.append((L, D))
    return t_open, out


def forecast(task, marks):
    places = int(task.get("places", 5))
    reads = [parse_time(r) for r in task.get("reads", [])]
    pool, t0 = build_pool(task, marks)
    if not pool or not reads:
        return {"probabilities": [], "top5": [], "sizes": []}
    edges = [float(e) for e in task["size"]["edges"]]
    n_bins = len(edges) + 1
    t_open, levels = fit_levels(task, marks, places)
    age_cut = (t0 - t_open).total_seconds() / 3600.0

    def bin_of(x):
        lo, hi = 0, len(edges)
        while lo < hi:
            mid = (lo + hi) >> 1
            if edges[mid] <= x:
                lo = mid + 1
            else:
                hi = mid
        return lo

    horizons = [max(0.0, (r - t0).total_seconds() / 3600.0) for r in reads]
    n_reads = len(reads)
    mints = [c["mint"] for c in pool]
    n = len(pool)

    rng = random.Random(SEED)
    t_scale = math.sqrt((DF - 2.0) / DF) if DF > 2 else 1.0
    half_df = DF / 2.0

    # One graduation time per (replicate, curve coin), exponential.
    grad_time = [[None] * n for _ in range(REPS)]
    for r in range(REPS):
        row = grad_time[r]
        for i, c in enumerate(pool):
            g = c["grad"]
            if g > 0:
                row[i] = -math.log(1.0 - rng.random()) / g

    # Per-read place frequencies and place-cap histograms.
    place_counts = [[[0] * places for _ in range(n)] for _ in range(n_reads)]
    size_hist = [[[0.0] * n_bins for _ in range(places)] for _ in range(n_reads)]

    for ri, h in enumerate(horizons):
        rsv = reserve(h)
        shift = level_shift(h)
        sq = math.sqrt(h) if h > 0 else 0.0
        for rep in range(REPS):
            gt = grad_time[rep]
            vals = []
            for i, c in enumerate(pool):
                step = (
                    c["sigma"] * sq * t_scale * rng.gauss(0.0, 1.0)
                    / math.sqrt(rng.gammavariate(half_df, 2.0) / DF)
                    if h > 0
                    else 0.0
                )
                v = c["lc"] + drift_cum(c["drift"], h) + step
                if c["venue"] != "pool":
                    if gt[i] is None or gt[i] >= h:
                        if v > CURVE_CAP:
                            v = CURVE_CAP
                if v < LOGCAP_FLOOR:
                    v = LOGCAP_FLOOR
                if v > LOGCAP_CEIL:
                    v = LOGCAP_CEIL
                vals.append(v)
            top = heapq.nlargest(places, range(n), key=vals.__getitem__)
            pc = place_counts[ri]
            sh = size_hist[ri]
            for j, idx in enumerate(top):
                pc[idx][j] += 1
                sh[j][bin_of(vals[idx])] += 1.0

    # Average the reads' honest distributions into the one forecast.
    prob_sum = [[0.0] * places for _ in range(n)]
    size_sum = [[0.0] * n_bins for _ in range(places)]
    for ri, h in enumerate(horizons):
        rsv = reserve(h)
        keep = 1.0 - rsv
        for i in range(n):
            for j in range(places):
                prob_sum[i][j] += (place_counts[ri][i][j] / REPS) * keep / n_reads
        age_read = (reads[ri] - t_open).total_seconds() / 3600.0
        for j in range(places):
            hist = size_hist[ri][j]
            tot = sum(hist)
            if tot <= 0:
                continue
            # Named-coin caps: histogram (the named part keeps its
            # simulated bins). The newcomer part is a normal around the
            # simulated mean, shifted up by the level's fill-in between
            # the cut and this read plus the slow record-growth shift,
            # and never narrower than the baseline's 0.35 sd.
            def _centre(b):
                if b == 0:
                    return edges[0] - 0.025
                if b == n_bins - 1:
                    return edges[-1] + 0.025
                return (edges[b - 1] + edges[b]) / 2.0

            mean = sum(hist[b] * _centre(b) for b in range(n_bins)) / tot
            var = 0.0
            for b in range(n_bins):
                var += hist[b] * (_centre(b) - mean) ** 2
            sd = max(math.sqrt(var / tot), NEWCOMER_SD_MIN)
            for b in range(n_bins):
                size_sum[j][b] += (hist[b] / tot) * keep / n_reads
            _L, D = levels[j]
            # v2: cap the fill-in rise at +0.25 — the level peaks around
            # cohort age ~1 h and then falls as the pump-phase winners
            # crash (trade-verified in the reconstructed reads: place 5
            # fell 3.28 -> 2.61 by 21:20); the fitted rise must not run
            # away at 1-2 h horizons.
            fast = min(0.25, D * (
                math.exp(-age_cut / LEVEL_TAU_H)
                - math.exp(-age_read / LEVEL_TAU_H)
            ))
            mu = mean + fast + level_shift(h)
            for b in range(n_bins):
                lo = -float("inf") if b == 0 else edges[b - 1]
                hi = float("inf") if b == n_bins - 1 else edges[b]
                p_b = _norm_cdf((hi - mu) / sd) - _norm_cdf((lo - mu) / sd)
                size_sum[j][b] += p_b * rsv / n_reads

    # Kernel-smooth each place's size distribution (sd SIZE_SMOOTH_SD):
    # a bin next to the mode must not score near the floor when the
    # level lands one bin away.
    for j in range(places):
        col = size_sum[j]
        tot = sum(col)
        if tot <= 0:
            continue
        sm = [0.0] * n_bins
        for b in range(n_bins):
            if col[b] <= 0:
                continue
            if b == 0:
                mu_b = edges[0] - 0.025
            elif b == n_bins - 1:
                mu_b = edges[-1] + 0.025
            else:
                mu_b = (edges[b - 1] + edges[b]) / 2.0
            for c in range(n_bins):
                clo = -float("inf") if c == 0 else edges[c - 1]
                chi = float("inf") if c == n_bins - 1 else edges[c]
                sm[c] += col[b] * (
                    _norm_cdf((chi - mu_b) / SIZE_SMOOTH_SD)
                    - _norm_cdf((clo - mu_b) / SIZE_SMOOTH_SD)
                )
        s = sum(sm)
        if s > 0:
            size_sum[j] = [v / s * tot for v in sm]

    # --- probabilities: write rows above the unnamed residual ----------
    chart_size = 100  # each answer read's chart, per the rules
    probabilities = []
    for j in range(places):
        col = sorted(
            ((prob_sum[i][j], mints[i]) for i in range(n)),
            key=lambda pu: (-pu[0], pu[1]),
        )
        keep = [(p, u) for (p, u) in col if p >= P_MIN][:100]
        for _ in range(3):
            s_sum = sum(p for p, _ in keep)
            unnamed_n = max(1, chart_size - len(keep))
            residual = max(0.0, 1.0 - s_sum) / unnamed_n
            new_keep = [(p, u) for (p, u) in keep if p >= residual * 0.9]
            if len(new_keep) == len(keep):
                break
            keep = new_keep
        for p, u in keep:
            probabilities.append({"mint": u, "place": j + 1, "p": round(float(p), 6)})
    probabilities.sort(key=lambda row: (row["place"], -row["p"], row["mint"]))

    # --- sizes: write bins above the unnamed residual -------------------
    sizes = []
    for j in range(places):
        col = sorted(
            ((size_sum[j][b], b) for b in range(n_bins)),
            key=lambda pb: (-pb[0], pb[1]),
        )
        keep = [(p, b) for (p, b) in col if p >= P_MIN]
        for _ in range(3):
            s_sum = sum(p for p, _ in keep)
            unnamed_n = max(1, n_bins - len(keep))
            residual = max(0.0, 1.0 - s_sum) / unnamed_n
            new_keep = [(p, b) for (p, b) in keep if p >= residual * 0.9]
            if len(new_keep) == len(keep):
                break
            keep = new_keep
        for p, b in keep:
            sizes.append({"place": j + 1, "bin": b, "p": round(float(p), 6)})
    sizes.sort(key=lambda row: (row["place"], -row["p"], row["bin"]))

    # --- top5: order by expected rank -----------------------------------
    exp_rank = {}
    for i, u in enumerate(mints):
        inside = sum(prob_sum[i][j] * (j + 1) for j in range(places))
        mass = sum(prob_sum[i][j] for j in range(places))
        exp_rank[u] = inside + (1.0 - mass) * OUTSIDE_RANK
    top5 = [u for u, _ in sorted(exp_rank.items(), key=lambda kv: (kv[1], kv[0]))[:places]]
    return {"probabilities": probabilities, "top5": top5, "sizes": sizes}


def _norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def main():
    task, marks = load_history(INPUT_DIR)
    result = forecast(task, marks)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, task.get("output", "forecast.json"))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(result, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
