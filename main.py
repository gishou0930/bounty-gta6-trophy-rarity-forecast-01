"""Forecast Exophase trophy earn rates from the snapshots in one case."""

import json
import math
import os
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from itertools import accumulate, product
from pathlib import Path
from statistics import median
from typing import NamedTuple


SQRT_HALF = math.sqrt(.5)

# Published launch-refresh totals in the make.fun kit's competition.yaml.
LAUNCH_PLAYER_GROWTH = 3868 / 638
LAUNCH_EXTRA_UNLOCKS_PER_PLAYER = ((4276 - 3868) - (943 - 638)) / (3868 - 638)
LAUNCH_REFRESH_HOURS = ((4.08, .25), (4.25, .50), (4.42, .25))

# Approximate cumulative gameplay hours at mandatory story milestones.
STORY_HOURS = {3: 12, 4: 21, 5: 39, 6: 55, 7: 62, 8: 15,
               9: 34, 10: 42, 11: 46, 12: 49, 13: 58}

# Optional trophy priors: effective effort in gameplay hours and the chance of
# pursuing it after enough playtime. See the README for how these are used.
SIDE_PRIORS = {
    1: (180, .09), 14: (155, .15), 15: (72, .09), 16: (38, .10),
    17: (38, .09), 18: (50, .06), 19: (44, .10), 20: (5, .38),
    21: (6, .10), 22: (31, .10), 23: (25, .10), 24: (26, .12),
    25: (66, .10), 26: (27, .12), 27: (32, .10), 28: (45, .08),
    29: (23, .14), 30: (40, .055), 31: (48, .08), 32: (64, .10),
    33: (17, .19), 34: (21, .10), 35: (14, .22), 36: (13, .11),
    37: (14, .08), 38: (8, .06), 39: (29, .13), 40: (55, .10),
    41: (30, .13), 42: (38, .13), 43: (43, .11), 44: (13, .06),
    45: (28, .10), 46: (18, .24), 47: (33, .08), 48: (32, .15),
    49: (31, .07), 50: (7, .07), 51: (12, .07), 52: (42, .13),
    53: (31, .18), 54: (29, .13), 55: (30, .10),
}


class Snapshot(NamedTuple):
    hour: float
    players: int
    rates: list[float | None]


def elapsed_hours(stamp, release):
    return (datetime.fromisoformat(stamp) - release).total_seconds() / 3600


def unique_snapshots(history, release):
    snapshots = []
    previous = None
    for read in history:
        state = (read['players_tracked'], read['trophies_earned'], read['rates'])
        if state != previous:
            snapshots.append(Snapshot(elapsed_hours(read['slot'], release), state[0], state[2]))
            previous = state
    return snapshots


def odds_log(p):
    p = min(1 - 1e-7, max(1e-7, p))
    return math.log(p / (1 - p))


def normal_cdf(z):
    return math.erfc(-z * SQRT_HALF) / 2


def mixture_quantiles(components, levels):
    """Invert the CDF of weighted logit-normal forecasts, then round as displayed."""
    mass = sum(weight for weight, _, _ in components)
    result = []
    for level in levels:
        low, high = -21.0, 21.0
        for _ in range(24):
            mid = (low + high) / 2
            cdf = sum(weight * normal_cdf((mid - center) / scale)
                      for weight, center, scale in components)
            if cdf < level * mass:
                low = mid
            else:
                high = mid
        result.append(round(100 / (1 + math.exp(-(low + high) / 2)), 2))
    return result


def arrivals(snapshots):
    """Put player-count increases into hourly cohorts between tracker refreshes."""
    cohorts = defaultdict(float)
    previous_hour, previous_count = snapshots[0].hour - 1, 0
    for snap in snapshots:
        added = snap.players - previous_count
        if added > 0:
            intervals = max(1, math.ceil(snap.hour - previous_hour))
            for step in range(intervals):
                midpoint = previous_hour + (snap.hour - previous_hour) * (step + .5) / intervals
                cohorts[math.floor(midpoint) + .5] += added / intervals
            previous_count = snap.players
        previous_hour = snap.hour
    return tuple(cohorts.items())


def projected_arrivals(snapshots, target, multiplier):
    latest = snapshots[-1]
    earlier = next((s for s in reversed(snapshots) if s.hour <= latest.hour - 8), snapshots[0])
    overall = latest.players / max(10, latest.hour + 4)
    recent = max(0, latest.players - earlier.players) / max(4, latest.hour - earlier.hour)
    pace = .35 * overall + .65 * min(recent, 2 * overall)
    step = max(0, target - latest.hour) / 16
    future = []
    for index in range(16):
        hour = latest.hour + (index + .5) * step
        slowdown = ((max(0, latest.hour) + 16) / (max(0, hour) + 16)) ** 1.15
        future.append((hour, multiplier * pace * slowdown * step))
    return tuple(future)


def eligible(cohorts, hour, effort, pace):
    """Expected eligible players under a distribution of cumulative play hours."""
    day_hours, exponent, spread = pace
    total = 0.0
    for joined, count in cohorts:
        age = hour - joined
        # The fastest players still need real elapsed time to reach a late trophy.
        feasible = min(1, (age / effort - .38) / .48)
        if feasible > 0:
            progress = day_hours * (age / 24) ** exponent
            total += count * feasible * normal_cdf(math.log(progress / effort) / spread)
    return total


def trophy_prior(trophy):
    trophy_id = trophy['id']
    if trophy_id in STORY_HOURS:
        return STORY_HOURS[trophy_id], .90
    fallback = (36, .12) if trophy['tier'] == 'bronze' else (64, .07)
    return SIDE_PRIORS.get(trophy_id, fallback)


def launch_quantiles(rates, universal, priors, players, last_read, target, levels):
    """Distribute the expected launch sync's unlocks over integer trophy counts."""
    population = round(players * LAUNCH_PLAYER_GROWTH)
    extra = (population - players) * LAUNCH_EXTRA_UNLOCKS_PER_PLAYER
    initial_counts = [round(rate * players / 100) for rate in rates]
    reviewer_weights = [0.0 if fixed else math.sqrt(count + .5)
                        for count, fixed in zip(initial_counts, universal)]
    reviewer_total = sum(reviewer_weights) or 1.0
    scenarios = [(hour, probability) for hour, probability in LAUNCH_REFRESH_HOURS
                 if hour > last_read]
    if not scenarios:
        scenarios = [(last_read + .5, .35), (last_read + 1.5, .65)]

    masses = [defaultdict(float) for _ in rates]
    for hour, scenario_mass in scenarios:
        if hour > target:
            for rate, mass in zip(rates, masses):
                mass[rate] += scenario_mass
            continue

        # Early adopters get the longest play window before the first sync.
        cohorts = ((0.0, .40), (.20 * hour, .28), (.45 * hour, .20), (.75 * hour, .12))
        play_weights = [0.0 if fixed else pursuit * eligible(cohorts, hour, effort, (8.0, .88, .82))
                        for fixed, (effort, pursuit) in zip(universal, priors)]
        play_total = sum(play_weights)
        for reviewer_share, share_mass in ((.54, .25), (.69, .50), (.84, .25)):
            weight = scenario_mass * share_mass
            for index, (count, fixed) in enumerate(zip(initial_counts, universal)):
                if fixed:
                    masses[index][100] += weight
                    continue
                reviewer_fraction = reviewer_weights[index] / reviewer_total
                play_fraction = play_weights[index] / play_total if play_total else reviewer_fraction
                expected = count + extra * (reviewer_share * reviewer_fraction + (1 - reviewer_share) * play_fraction)
                lower = math.floor(expected)
                upper_weight = expected - lower
                masses[index][round(100 * min(lower, population) / population, 2)] += weight * (1 - upper_weight)
                masses[index][round(100 * min(lower + 1, population) / population, 2)] += weight * upper_weight

    forecasts = []
    for mass in masses:
        ordered = sorted(mass)
        cumulative = list(accumulate(mass[rate] for rate in ordered))
        forecasts.append([ordered[bisect_left(cumulative, level * cumulative[-1])] for level in levels])
    return forecasts


def observed_count(rate, snapshot):
    return rate * snapshot.players / 100


def launch_seed(reads, first, latest):
    if reads[0][0].hour > 1 or latest.players < 1.7 * first.players:
        return 0.0
    # Further reads can lose early players as the tracked population changes.
    # Bound the seed with the upper rounding limit rather than treating zero
    # printed percent as proof of zero unlocks in a very large population.
    return .85 * min(observed_count(rate + .005, snap) for snap, rate in reads)


def story_error(samples, story, pace):
    total_error, total_weight = 0.0, 0.0
    for snap, cohorts in samples:
        for index, effort, seed in story:
            rate = snap.rates[index]
            if rate is None:
                continue
            count = observed_count(rate, snap)
            predicted = seed + .90 * eligible(cohorts, snap.hour, effort, pace)
            # Allow for the first handful of unlocks and rounded rates.
            tolerance = 1.5 + snap.players / 20_000
            error = abs(math.log((count + tolerance) / (predicted + tolerance)))
            weight = min(1, max(2, count - seed) / 16)
            total_weight += weight
            total_error += weight * min(3, error) ** 1.4
    return total_error / max(1e-9, total_weight)


def observed_trend(reads, seed, target):
    """Conservatively extend an established rate in log age and log odds."""
    recent, rate = reads[-1]
    current_logit = odds_log(rate / 100)
    slopes = [
        (current_logit - odds_log(older_rate / 100))
        / math.log((recent.hour + 16) / (older.hour + 16))
        for older, older_rate in reads
        if older.hour >= 4 and 5 <= recent.hour - older.hour <= 24
        and observed_count(older_rate, older) >= seed + 15
    ]
    if len(slopes) < 2 or observed_count(rate, recent) < seed + 30:
        return None
    slope = max(-.9, min(2.6, median(slopes)))
    advance = math.log((target + 16) / (recent.hour + 16))
    return current_logit + .8 * slope * advance


def forecast(case):
    release = datetime.fromisoformat(case['game']['release_date'] + 'T00:00:00+00:00')
    target = elapsed_hours(case['target'], release)
    checkpoint = elapsed_hours(case['at'], release)
    horizon = case['horizon_minutes'] / 60
    levels = case['levels']
    snapshots = unique_snapshots(case['history'], release)
    first, latest = snapshots[0], snapshots[-1]
    trophies = case['trophies']
    priors = [trophy_prior(trophy) for trophy in trophies]
    observations = [[(snap, snap.rates[index]) for snap in snapshots
                     if snap.rates[index] is not None] for index in range(len(trophies))]
    universal = [bool(reads) and reads[-1][0].players >= 100 and all(rate == 100 for _, rate in reads)
                 for reads in observations]

    launch_only = latest.hour <= 1 and checkpoint <= 12 and latest.players < 1.7 * first.players
    if launch_only:
        last_read = elapsed_hours(case['history'][-1]['slot'], release)
        rates = [reads[-1][1] if reads else 0.0 for reads in observations]
        launch_rows = launch_quantiles(rates, universal, priors, latest.players, last_read, target, levels)
        return {'trophies': [{'id': trophy['id'], 'q': quantiles}
                             for trophy, quantiles in zip(trophies, launch_rows)]}

    seeds = [launch_seed(reads, first, latest) if reads else 0.0 for reads in observations]
    usable = [i for i, snap in enumerate(snapshots) if snap.hour >= 3]
    usable = usable or [len(snapshots) - 1]
    stride = max(1, math.ceil(len(usable) / 7))
    sample_indices = usable[::-stride]
    samples = [(snapshots[index], arrivals(snapshots[:index + 1])) for index in sample_indices]
    story = [(index, priors[index][0], seeds[index]) for index, trophy in enumerate(trophies)
             if trophy['id'] in STORY_HOURS]
    paces = list(product((3.25, 4.8, 6.4, 8.1, 10.4), (.78, .96, 1.12), (.53, .75, .98)))
    errors = [story_error(samples, story, pace) for pace in paces]
    best_error = min(errors)
    weights = [math.exp(-6 * (error - best_error)) for error in errors]
    weight_total = sum(weights)
    current = arrivals(snapshots)
    futures = []
    for growth, probability in ((.70, .25), (1.0, .50), (1.45, .25)):
        future = current + projected_arrivals(snapshots, target, growth)
        futures.append((future, sum(count for _, count in future), probability))

    output = []
    for trophy, reads, (effort, pursuit), seed, fixed in zip(trophies, observations, priors, seeds, universal):
        if fixed:
            quantiles = [100] * len(levels)
        else:
            snap, rate = reads[-1] if reads else (latest, 0.0)
            successes = max(0, observed_count(rate, snap) - seed)
            tracked = current if snap is latest else arrivals([s for s in snapshots if s.hour <= snap.hour])
            spread = .22 + .22 * math.sqrt(horizon / 24) + .45 / math.sqrt(1 + successes / 12)
            components = []
            for pace, weight in zip(paces, weights):
                present = eligible(tracked, snap.hour, effort, pace)
                participation = min(1, (successes + 14 * pursuit) / (present + 14))
                for future, population, probability in futures:
                    reachable = eligible(future, target, effort, pace)
                    prediction = (seed + participation * reachable) / population
                    components.append((weight * probability, odds_log(prediction), spread))
            trend = observed_trend(reads, seed, target) if reads else None
            if trend is not None:
                components.append((.22 * weight_total, trend, spread * 1.30))
            quantiles = mixture_quantiles(components, levels)
        output.append({'id': trophy['id'], 'q': quantiles})
    return {'trophies': output}


def main():
    input_dir = Path(os.environ.get('MAKEFUN_INPUT_DIR', '/input'))
    output_dir = Path(os.environ.get('MAKEFUN_OUTPUT_DIR', '/output'))
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(input_dir.glob('*.in')):
        case = json.loads(path.read_text(encoding='utf-8'))
        result = json.dumps(forecast(case), separators=(',', ':'), allow_nan=False)
        (output_dir / f'{path.stem}.out').write_text(result + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
