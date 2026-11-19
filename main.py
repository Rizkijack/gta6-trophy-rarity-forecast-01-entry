"""Refresh-aware damped-trend quantile forecaster for GTA VI trophy earn rates."""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
TIER_MAX_DELTA = {"platinum": 1.5, "gold": 2.0, "silver": 2.5, "bronze": 3.0}


def parse_dt(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def clamp_pct(x):
    return min(99.99, max(0.01, x))


def logit(pct):
    p = clamp_pct(pct) / 100.0
    return math.log(p / (1.0 - p))


def sigmoid_logit_to_pct(lg):
    if lg >= 20:
        return 100.0
    if lg <= -20:
        return 0.0
    return 100.0 / (1.0 + math.exp(-lg))


def round4(x):
    scaled = x * 10000.0
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 10000.0
    return int(value) if value.is_integer() else value


def weighted_median(values, weights):
    if not values:
        return 0.0
    pairs = sorted(zip(values, weights), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    half = total / 2.0
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def collapse_observations(history):
    out = []
    for read in history:
        rates = read.get("rates")
        key = (read.get("players_tracked"), tuple(rates) if rates is not None else None)
        if out:
            prev = out[-1]
            prev_key = (prev["players_tracked"], tuple(prev["rates"]) if prev["rates"] is not None else None)
            if key == prev_key:
                continue
        out.append(read)
    return out


def series_for_trophy(obs, index):
    release = None
    points = []
    for read in obs:
        rates = read.get("rates")
        if not rates or index >= len(rates):
            continue
        rate = rates[index]
        if rate is None:
            continue
        if release is None:
            release = None
        slot = read.get("slot") or read.get("at")
        x = parse_dt(slot)
        points.append((x, float(rate)))
    return points


def fit_slope(points, last_x):
    if len(points) < 2:
        return 0.0
    xs = [p[0].timestamp() / 3600.0 for p in points]
    ys = [logit(p[1]) for p in points]
    x_last = last_x.timestamp() / 3600.0
    slopes = []
    weights = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dx = xs[j] - xs[i]
            if dx < 0.05:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
            # Weight by how recent the later point is.
            w = math.exp(-(x_last - xs[j]) / 12.0)
            if w <= 0:
                w = 1e-12
            weights.append(w)
    if not slopes:
        return 0.0
    slope = weighted_median(slopes, weights)
    # Keep the fit from becoming physically absurd.
    return max(-0.35, min(0.35, slope))


def growth_ratio(obs, horizon_hours):
    tracked_points = []
    for read in obs:
        t = read.get("players_tracked")
        if t is None:
            continue
        slot = read.get("slot") or read.get("at")
        tracked_points.append((parse_dt(slot), float(t)))
    if len(tracked_points) < 2:
        # Flat launch-night style history: the unseen first refresh is large.
        return 6.0
    xs = [p[0].timestamp() / 3600.0 for p in tracked_points]
    ys = [math.log(max(1.0, p[1])) for p in tracked_points]
    x_last = xs[-1]
    slopes = []
    weights = []
    for i in range(len(tracked_points)):
        for j in range(i + 1, len(tracked_points)):
            dx = xs[j] - xs[i]
            if dx < 0.1:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
            weights.append(math.exp(-(x_last - xs[j]) / 24.0))
    if not slopes:
        return 1.0
    r = weighted_median(slopes, weights)
    r = max(0.0, min(0.08, r))
    ratio = math.exp(r * horizon_hours)
    return max(1.0, min(25.0, ratio))


def tracked_at(dt, obs):
    for read in obs:
        slot = read.get("slot") or read.get("at")
        if parse_dt(slot) == dt:
            t = read.get("players_tracked")
            return float(t) if t is not None else None
    # Fall back to nearest earlier observation.
    best = None
    for read in obs:
        slot = read.get("slot") or read.get("at")
        rdt = parse_dt(slot)
        if rdt <= dt and (best is None or rdt > best[0]):
            best = (rdt, read)
    if best is not None:
        t = best[1].get("players_tracked")
        return float(t) if t is not None else None
    return None


def tier_pooled_deltas(obs_list, tier_by_index):
    by_tier = {t: [] for t in ("platinum", "gold", "silver", "bronze")}
    for idx, trophy in enumerate([]):
        pass
    # This helper is replaced by inline pooling in forecast_case; keep for compatibility.
    return by_tier


def forecast_case(case):
    release_date = case.get("game", {}).get("release_date", "2026-11-19")
    try:
        release_dt = parse_dt(release_date + "T00:00:00Z")
    except Exception:
        release_dt = parse_dt("2026-11-19T00:00:00Z")

    at_dt = parse_dt(case["at"])
    target_dt = parse_dt(case["target"])
    horizon_minutes = float(case.get("horizon_minutes", 0))
    horizon_hours = horizon_minutes / 60.0
    horizon_days = horizon_minutes / 1440.0
    sqrt_days = math.sqrt(max(0.0, horizon_days))

    obs = collapse_observations(case.get("history", []))
    flat_history = len(obs) <= 1

    # Pooled refresh deltas by tier.
    pooled = {"platinum": [], "gold": [], "silver": [], "bronze": []}
    trophy_series = []
    for i, trophy in enumerate(case["trophies"]):
        pts = series_for_trophy(obs, i)
        trophy_series.append(pts)
        deltas = []
        for k in range(1, len(pts)):
            deltas.append(logit(pts[k][1]) - logit(pts[k - 1][1]))
        tier = trophy.get("tier", "bronze")
        if tier not in pooled:
            tier = "bronze"
        pooled[tier].extend(deltas)
    all_deltas = [d for vals in pooled.values() for d in vals]

    R = growth_ratio(obs, horizon_hours)
    logR = math.log(R)

    medians = [0.0] * len(case["trophies"])
    sigmas = [0.0] * len(case["trophies"])
    last_rates = [None] * len(case["trophies"])

    for i, trophy in enumerate(case["trophies"]):
        tier = trophy.get("tier", "bronze")
        if tier not in TIER_MAX_DELTA:
            tier = "bronze"
        secret = bool(trophy.get("secret", False))
        pts = trophy_series[i]

        if pts:
            last_rate = pts[-1][1]
            last_x = pts[-1][0]
            current_logit = logit(last_rate)
        else:
            # Trophy missing from every read: use a tiny prior.
            last_rate = 0.01
            last_x = obs[-1]["slot"] if obs else case["at"]
            last_x = parse_dt(last_x) if isinstance(last_x, str) else last_x
            current_logit = logit(0.01)

        last_rates[i] = last_rate
        slope = fit_slope(pts, last_x)
        raw_delta = slope * (target_dt.timestamp() / 3600.0 - last_x.timestamp() / 3600.0)
        max_delta = TIER_MAX_DELTA[tier]
        if secret:
            max_delta *= 0.75
        if max_delta > 0:
            delta = max_delta * math.tanh(raw_delta / max_delta)
        else:
            delta = 0.0

        # Denominator dynamics. Estimate whether this trophy dilutes (new tracked players
        # have not earned it) or progresses (players keep advancing) from refresh deltas.
        betas = []
        for k in range(1, len(pts)):
            prev_rate = clamp_pct(pts[k - 1][1])
            next_rate = clamp_pct(pts[k][1])
            prev_t = tracked_at(pts[k - 1][0], obs)
            next_t = tracked_at(pts[k][0], obs)
            if prev_t is None or next_t is None or prev_t <= 0 or next_t <= prev_t:
                continue
            dln_t = math.log(next_t / prev_t)
            if dln_t <= 0:
                continue
            dln_r = math.log(next_rate / prev_rate)
            betas.append(dln_r / dln_t)
        if betas:
            beta = weighted_median(betas, [1.0] * len(betas))
        else:
            beta = 0.0

        if tier == "platinum":
            c = -0.35
        elif flat_history:
            # Unseen first refresh: every rate is diluted by the tracked jump.
            c = -0.85
        elif beta < 0.25:
            c = -0.55
        elif beta > 0.85:
            c = 0.35
        else:
            c = -0.10
        if tier in ("gold", "silver") and not flat_history:
            c += 0.08
        if secret:
            c -= 0.08
        shift = c * logR

        median_logit = current_logit + delta + shift

        # Spread from observed refresh moves, pooled by tier when sparse.
        own_deltas = []
        for k in range(1, len(pts)):
            own_deltas.append(logit(pts[k][1]) - logit(pts[k - 1][1]))
        if len(own_deltas) >= 3:
            use_deltas = own_deltas
        else:
            use_deltas = pooled.get(tier, [])
            if len(use_deltas) < 3:
                use_deltas = all_deltas
        if flat_history or not use_deltas:
            obs_sigma = None
        else:
            obs_sigma = math.sqrt(sum(d * d for d in use_deltas) / len(use_deltas))

        baseline_spread = 0.5 * sqrt_days
        if obs_sigma is None:
            sigma = 1.25 * baseline_spread + 0.1 * sqrt_days + 0.05
        else:
            sigma = max(obs_sigma * sqrt_days, 0.4 * sqrt_days) + 0.1 * sqrt_days
        # Rare/secret trophies get a somewhat wider prior.
        rarity_factor = 1.0
        if last_rate < 50:
            rarity_factor += 0.3 * (1.0 - last_rate / 50.0)
        if secret:
            rarity_factor *= 1.15
        sigma *= rarity_factor
        if flat_history:
            sigma = max(sigma, baseline_spread * 1.05 + 1e-9)
        sigma = max(1e-6, min(4.0, sigma))

        medians[i] = median_logit
        sigmas[i] = sigma

    # Structural prior: platinum rate <= min(other rates).
    other_indices = [i for i, t in enumerate(case["trophies"]) if t.get("tier") != "platinum"]
    plat_indices = [i for i, t in enumerate(case["trophies"]) if t.get("tier") == "platinum"]
    if plat_indices and other_indices:
        other_min = min(medians[i] for i in other_indices)
        for i in plat_indices:
            medians[i] = min(medians[i], other_min - 0.05)

    # Secret trophies lag visible trophies of a similar tier.
    for i, trophy in enumerate(case["trophies"]):
        if not trophy.get("secret", False):
            continue
        tier = trophy.get("tier", "bronze")
        visible = [medians[j] for j, t in enumerate(case["trophies"])
                   if t.get("tier") == tier and not t.get("secret", False)]
        if visible:
            medians[i] = min(medians[i], min(visible) - 0.10)

    rows = []
    for i, trophy in enumerate(case["trophies"]):
        qs = []
        for z in Z:
            lg = medians[i] + z * sigmas[i]
            qs.append(sigmoid_logit_to_pct(lg))
        # Clamp and enforce monotonicity after rounding.
        qs = [max(0.0, min(100.0, q)) for q in qs]
        rounded = [round4(q) for q in qs]
        for k in range(1, len(rounded)):
            if rounded[k] < rounded[k - 1]:
                rounded[k] = rounded[k - 1]
        rows.append({"id": trophy["id"], "q": rounded})

    return {"trophies": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        out_path = OUTPUT_DIR / f"{path.stem}.out"
        out_path.write_text(json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
