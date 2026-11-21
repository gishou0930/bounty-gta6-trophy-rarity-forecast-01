"""Forecast Exophase trophy earn rates from the snapshots in one case."""

import json
import math
import os
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from functools import cache
from itertools import accumulate, product
from pathlib import Path
from random import Random
from statistics import median
from typing import NamedTuple


SQRT_HALF = math.sqrt(.5)

# Published launch-refresh totals in the make.fun kit's competition.yaml.
LAUNCH_PLAYER_GROWTH = 3868 / 638
LAUNCH_EXTRA_UNLOCKS_PER_PLAYER = ((4276 - 3868) - (943 - 638)) / (3868 - 638)
LAUNCH_REFRESH_HOURS = ((4.08, .25), (4.25, .50), (4.42, .25))
LAUNCH_PERSISTENCE_LOSS = 1.51
LAUNCH_PERSISTENCE_MISS = .68
PERSISTENCE_NORMAL_QUANTILES = (-1.645, -.674, 0, .674, 1.645)

LAUNCH_LEVELS = (.05, .25, .50, .75, .95)
# Calibrated on simulated refreshes that meet the published aggregate counts.
# Rows follow the launch groups: main story, short, medium, long activities.
LAUNCH_GROUP_LEVELS = (
    (.02, .18, .60, .82, .98),
    (.02, .285, .60, .82, .98),
    (.02, .18, .40, .785, .965),
    (.02, .18, .40, .715, .90),
)

# PS5 release rolled through local midnight, beginning at 11:00 UTC the prior
# day. These weights approximate the early regions represented at the sync.
REGIONAL_STARTS = ((-13.0, .04), (-10.5, .08), (-8.5, .18), (-5.5, .08),
                   (-2.5, .09), (-1.0, .34), (0.0, .15), (3.0, .04))
PLAY_START_DELAYS = ((.25, .48), (1.50, .35), (3.50, .17))
PROFILE_SCAN_DELAYS = ((.40, .30), (2.20, .45), (4.70, .25))

# Approximate cumulative gameplay hours at mandatory story milestones.
STORY_HOURS = {3: 12, 4: 21, 5: 39, 6: 55, 7: 62, 8: 15,
               9: 34, 10: 42, 11: 46, 12: 49, 13: 58}

# Trophy ids map to awards that normally require them, including the
# completed-story and side-activity requirements for A Long Road.
UNLOCK_PREREQUISITES = {
    3: (4, 8, 24, 28, 29, 33, 34, 47, 54, 55),
    4: (5, 9, 22, 26, 27, 39, 45, 49, 52),
    5: (6, 10, 11, 12, 40),
    6: (7, 13),
    7: (14,),
    8: (4,),
    9: (5,),
    10: (6,),
    11: (6,),
    12: (6,),
    13: (7,),
    15: (14,),
    17: (14,),
    18: (14,),
    19: (14,),
    30: (14,),
    31: (14,),
    32: (14,),
    35: (14,),
    40: (14,),
    41: (14, 40),
    43: (14,),
    52: (14,),
}

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


def score_logit(rate):
    return odds_log(min(99.99, max(.01, rate)) / 100)


def persistence_logit(rate, normal_quantile):
    spread = .5 * math.sqrt(90 / 1440)
    shifted = score_logit(rate) + spread * normal_quantile
    prediction = 100 / (1 + math.exp(-shifted))
    return score_logit(math.floor(prediction * 10_000 + .5) / 10_000)


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


def condition_allocations(partial, play, groups, total, partial_share, fit, rng):
    """Weight paired player cohorts by their fit to the published refresh."""
    if not total:
        counts = [0] * len(partial)
        return fit(counts)[0], [(counts, 1.0)]

    reviewed = round(total * partial_share)
    public = total - reviewed
    indices = range(len(partial))
    group_masses = [sum(weight for weight, group in zip(partial, groups) if group == group_id)
                    for group_id in range(max(groups) + 1)]
    weighted, prior_mass = [], 0.0
    for _ in range(512):
        # Allow a batch of early profiles to favor a stage, while keeping
        # each stage's expected share proportional to the pre-refresh prior.
        factors = [rng.gammavariate(18 * mass, 1) / mass if mass else 0
                   for mass in group_masses]
        profile_weights = [weight * factors[group] for weight, group in zip(partial, groups)]
        counts = [0] * len(partial)
        for index in rng.choices(indices, weights=profile_weights, k=reviewed):
            counts[index] += 1
        for index in rng.choices(indices, weights=play, k=public):
            counts[index] += 1
        observed, coherent = fit(counts)
        weighted.append((counts, observed * coherent))
        prior_mass += coherent

    mass = sum(weight for _, weight in weighted)
    return mass / prior_mass, [(counts, weight / mass) for counts, weight in weighted]


def aggregate_bounds(costs_by_trophy, total):
    """Find the exact extremes under an integer award budget."""
    minimum = [0.0] + [math.inf] * total
    maximum = [0.0] + [-math.inf] * total
    for costs in costs_by_trophy:
        next_minimum = [math.inf] * (total + 1)
        next_maximum = [-math.inf] * (total + 1)
        for spent, (low, high) in enumerate(zip(minimum, maximum)):
            if low == math.inf:
                continue
            for added, cost in enumerate(costs[:total - spent + 1]):
                new_spent = spent + added
                next_minimum[new_spent] = min(next_minimum[new_spent], low + cost)
                next_maximum[new_spent] = max(next_maximum[new_spent], high + cost)
        minimum, maximum = next_minimum, next_maximum
    return minimum[-1], maximum[-1]


def launch_quantiles(trophies, rates, universal, priors, players, last_read, target, levels):
    """Forecast integer unlocks from complete profiles, partial profiles, and new play."""
    nonuniversal = universal.count(False)
    if nonuniversal == 0:
        return [[100] * len(levels) for _ in rates]
    scenarios = [(hour, probability) for hour, probability in LAUNCH_REFRESH_HOURS
                 if hour > last_read]
    if not scenarios:
        scenarios = [(last_read + .5, .35), (last_read + 1.5, .65)]
    if all(hour > target for hour, _ in scenarios):
        return [[rate] * len(levels) for rate in rates]

    population = round(players * LAUNCH_PLAYER_GROWTH)
    extra = round((population - players) * LAUNCH_EXTRA_UNLOCKS_PER_PLAYER)
    initial_counts = [round(rate * players / 100) for rate in rates]
    platinum = next((i for i, trophy in enumerate(trophies) if trophy['tier'] == 'platinum'), None)
    full_profiles = initial_counts[platinum] if platinum is not None else 0
    max_complete = extra // nonuniversal if full_profiles else 0
    full_scenarios = [(0, .35), (max_complete, .65)] if max_complete else [(0, 1.0)]
    if full_profiles and all(count > 0 for count, fixed in zip(initial_counts, universal) if not fixed):
        # The set of ranked profiles can change as the tracker refreshes.
        full_scenarios = [(change, .92 * weight) for change, weight in full_scenarios] + [(-1, .08)]

    story_indices = {i for i, trophy in enumerate(trophies) if trophy['id'] in STORY_HOURS and not universal[i]}
    can_add_story_profile = bool(story_indices and all(initial_counts[i] > full_profiles for i in story_indices))
    cohort_scenarios = []
    for full_change, full_weight in full_scenarios:
        after_full = extra - full_change * nonuniversal
        story_scenarios = ((0, .60), (1, .40)) if can_add_story_profile and after_full >= len(story_indices) else ((0, 1.0),)
        for story_change, story_weight in story_scenarios:
            start = [population if fixed else min(population, count + full_change + (story_change if i in story_indices else 0))
                     for i, (count, fixed) in enumerate(zip(initial_counts, universal))]
            cohort_scenarios.append((start, after_full - story_change * len(story_indices), full_weight * story_weight))

    partial_weights = [0 if fixed or i == platinum else
                       (max(0, count - full_profiles) + .35) ** .78 / math.sqrt(1 + effort / 45)
                       for i, (count, fixed, (effort, _)) in enumerate(zip(initial_counts, universal, priors))]
    if not any(partial_weights):
        partial_weights = [int(not fixed) for fixed in universal]
    partial_total = sum(partial_weights)
    groups = [0 if trophy['id'] in STORY_HOURS else 1 + bisect_left((15, 38), effort)
              for trophy, (effort, _) in zip(trophies, priors)]
    index_by_id = {trophy['id']: i for i, trophy in enumerate(trophies)}
    prerequisites = [(index_by_id[chapter], index_by_id[trophy])
                     for chapter, dependent in UNLOCK_PREREQUISITES.items()
                     if chapter in index_by_id
                     for trophy in dependent if trophy in index_by_id]

    # Apply the published baseline formula for the documented 90-minute
    # transition to each simulated allocation of the public aggregate count.
    persistence = [tuple(persistence_logit(rate, z) for z in PERSISTENCE_NORMAL_QUANTILES) for rate in rates]
    displayed = [round(100 * count / population, 2) for count in range(population + 1)]
    logits = [score_logit(rate) for rate in displayed]

    @cache
    def baseline_cost(index, count):
        errors = (logits[count] - quantile for quantile in persistence[index])
        return sum(2 * max(level * error, (level - 1) * error)
                   for level, error in zip(levels, errors))

    # The published zero coverage and rate declines cap each non-universal
    # count below the baseline's lower quantile. Exclude cohort assignments
    # whose best or worst feasible aggregate misses the published interval.
    caps = [population if fixed else bisect_left(logits, row[0]) - 1
            for fixed, row in zip(universal, persistence)]
    score_scale = len(rates) * len(levels)
    feasible = []
    for initial, remainder, weight in cohort_scenarios:
        costs = []
        for i, (first, cap) in enumerate(zip(initial, caps)):
            last = min(cap, first if i == platinum else first + remainder)
            costs.append(tuple(baseline_cost(i, count) for count in range(first, last + 1)))
        low, high = aggregate_bounds(costs, remainder)
        if (high / score_scale >= LAUNCH_PERSISTENCE_LOSS - .005
                and low / score_scale < LAUNCH_PERSISTENCE_LOSS + .005):
            feasible.append((initial, remainder, weight))
    if feasible:
        cohort_scenarios = feasible

    masses = [defaultdict(float) for _ in rates]
    rng = Random(0)
    for hour, scenario_mass in scenarios:
        if hour > target:
            for rate, mass in zip(rates, masses):
                mass[rate] += scenario_mass
            continue

        # Player starts and profile scans both shorten the usable play window.
        cohorts = tuple((start + delay + scan, region_weight * play_weight * scan_weight)
                        for start, region_weight in REGIONAL_STARTS
                        for delay, play_weight in PLAY_START_DELAYS
                        for scan, scan_weight in PROFILE_SCAN_DELAYS
                        if start + delay + scan < hour)
        play_weights = [0.0 if fixed or i == platinum else pursuit * eligible(cohorts, hour, effort, (8.0, .88, .82))
                        for i, (fixed, (effort, pursuit)) in enumerate(zip(universal, priors))]
        play_total = sum(play_weights)
        partial = [value / partial_total for value in partial_weights]
        play = [value / play_total for value in play_weights] if play_total else partial
        refreshed = [defaultdict(float) for _ in rates]
        refreshed_mass = 0.0
        for initial, remainder, cohort_mass in cohort_scenarios:
            def fit(increments):
                earned = [min(population, count + increase) for count, increase in zip(initial, increments)]
                loss = sum(baseline_cost(i, count) for i, count in enumerate(earned)) / score_scale
                miss = sum(abs(rate - displayed[count]) for rate, count in zip(rates, earned)) / len(rates)
                loss_gap = max(0.0, abs(loss - LAUNCH_PERSISTENCE_LOSS) - .005)
                miss_gap = max(0.0, abs(miss - LAUNCH_PERSISTENCE_MISS) - .005)
                above_band_floor = any(logits[count] >= persistence[i][0] for i, count in enumerate(earned)
                                       if not universal[i])
                out_of_order = sum(max(0, earned[trophy] - earned[chapter])
                                   for chapter, trophy in prerequisites)
                fit = math.exp(-.5 * ((loss_gap / .005) ** 2 + (miss_gap / .0025) ** 2))
                aggregate_fit = .002 + .998 * fit * (.04 if above_band_floor else 1.0)
                # Keep a small allowance for inconsistent tracker scans.
                return aggregate_fit, .02 + .98 * math.exp(-1.5 * out_of_order)

            for partial_share, share_mass in ((.28, .14), (.48, .24), (.68, .37), (.88, .25)):
                evidence, draws = condition_allocations(partial, play, groups, remainder, partial_share, fit, rng)
                scenario_weight = cohort_mass * share_mass * evidence
                refreshed_mass += scenario_weight
                for increments, conditional_weight in draws:
                    weight = scenario_weight * conditional_weight
                    earned = [min(population, count + increase) for count, increase in zip(initial, increments)]
                    for mass, count in zip(refreshed, earned):
                        mass[displayed[count]] += weight
        for mass, conditional in zip(masses, refreshed):
            for rate, weight in conditional.items():
                mass[rate] += weight * scenario_mass / refreshed_mass

    forecasts = []
    calibrate = tuple(levels) == LAUNCH_LEVELS
    for mass, group in zip(masses, groups):
        probabilities = LAUNCH_GROUP_LEVELS[group] if calibrate else levels
        ordered = sorted(mass)
        cumulative = list(accumulate(mass[rate] for rate in ordered))
        forecasts.append([ordered[bisect_left(cumulative, level * cumulative[-1])] for level in probabilities])
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
        launch_rows = launch_quantiles(trophies, rates, universal, priors, latest.players, last_read, target, levels)
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
