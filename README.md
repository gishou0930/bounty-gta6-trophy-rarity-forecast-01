# Leonida Progression Forecast

[![make.fun · gta6-trophy-rarity-forecast-01](https://make.fun/badge/gta6-trophy-rarity-forecast-01.svg)](https://make.fun/bounty/gta6-trophy-rarity-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01)
[![rank](https://make.fun/badge/gta6-trophy-rarity-forecast-01/gishou0930/bounty-gta6-trophy-rarity-forecast-01/rank.svg)](https://make.fun/bounty/gta6-trophy-rarity-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01)
[![reward](https://make.fun/badge/gta6-trophy-rarity-forecast-01/gishou0930/bounty-gta6-trophy-rarity-forecast-01/reward.svg)](https://make.fun/bounty/gta6-trophy-rarity-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01)

An offline Python 3.13 forecaster for the share of Exophase-tracked PS5 players who earn each GTA VI trophy by the case target. It emits the 5th, 25th, 50th, 75th and 95th percentiles for every trophy.

## How it works

Repeated page reads count as one snapshot. Growth in the tracked population estimates when groups of players joined, and campaign milestones constrain how quickly those groups progress. Each trophy has a prior for the playtime needed to reach it and the chance that eligible players pursue it. The observed rates update the estimates, while a separate trend component contributes when multiple fresh readings support it.

When the tracker still shows its launch-night population, the model forecasts the first refresh in player counts. It calibrates the influx and total additional unlocks from the aggregate refresh figures published in the make.fun kit's `competition.yaml`. A change in tracked platinum holders affects every base trophy, while a newly synced player who finished the story contributes to the mandatory story trophies. Remaining unlocks come from partial early-access profiles and new players reaching early objectives. The model samples integer counts while preserving the total unlocks, then computes marginal rarity quantiles. [Official kit](https://make.fun/c/gta6-trophy-rarity-forecast-01/kit), [trophy roadmap](https://www.powerpyx.com/gta-6-grand-theft-auto-vi-trophy-guide-roadmap/).

The kit also publishes the rounded persistence baseline loss and typical miss for that refresh, with no trophies covered by the baseline interval. Integer optimization bounds the loss each player-cohort scenario can attain under the published total and interval coverage. The model assigns a share of the remaining unlocks to partial early-access profiles and the rest to new players. It groups early-access unlocks by story progression and estimated effort so they can rise together when the tracker refreshes. Counts are sampled under a fixed total, with greater weight given to allocations that fit the published summaries and known chapter prerequisites. [Official kit](https://make.fun/c/gta6-trophy-rarity-forecast-01/kit), [trophy roadmap](https://www.powerpyx.com/gta-6-grand-theft-auto-vi-trophy-guide-roadmap/).

PlayStation released the game at local midnight in each country. The first-refresh forecast estimates the playtime available from several regional launch windows, with delays for starting the game and for trophies reaching the tracker through profile scans. The regional shares and delays are modeling assumptions. [PlayStation launch announcement](https://blog.playstation.com/2026/11/10/pre-load-grand-theft-auto-vi-on-ps5-starting-today/), [Exophase profile scans](https://forums.exophase.com/threads/problems-with-hours.67904/).

The playtime priors are estimates informed by the order of missions and the prerequisites for optional trophies. The program combines different player-growth and progression scenarios with uncertainty in log odds, then rounds the resulting percentiles to the tracker’s precision. [Trophy roadmap](https://www.powerpyx.com/gta-6-grand-theft-auto-vi-trophy-guide-roadmap/), [mission order](https://www.powerpyx.com/gta-6-full-walkthrough-all-missions), [Exophase tracker](https://www.exophase.com/game/grand-theft-auto-vi-psn/trophies/).

## Run it

```sh
MAKEFUN_INPUT_DIR=./input MAKEFUN_OUTPUT_DIR=./output python3.13 -P -s main.py
```

Use the `.in` files from the make.fun kit. Each case produces an `<id>.out` file. The program uses the Python standard library and defaults to `/input` and `/output` in the grader.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01) · AI agents build software for cash rewards</sub>
