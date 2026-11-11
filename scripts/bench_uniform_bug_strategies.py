#!/usr/bin/env python3
"""Compare strategies for making bug-*type* suggestions more uniform over a session.

Simulates many suggestion steps with realistic fit ratings (biased like real logs:
misinterpretation / missing_corner_case dominate) and optional plant-failure rates.
Does not call the LLM by default — selection policy is what Uniform must fix.

Usage (from repo root):
  python scripts/bench_uniform_bug_strategies.py
  python scripts/bench_uniform_bug_strategies.py --steps 40 --sessions 200 --checkpoints 5,10,20,40
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Built-in skills (same ids as bug_skills/*.md).
SKILLS = [
    "api_misuse",
    "hallucinated_object",
    "incomplete_generation",
    "misinterpretation",
    "missing_corner_case",
    "redundant_conditions",
    "syntax_error",
    "type_error",
    "undefined_name",
]

# Empirical plant preference from submissions/bug_injections.jsonl + Luna bench:
# when the model is free to pick, these dominate.
MODEL_PICK_WEIGHTS = {
    "misinterpretation": 36,
    "missing_corner_case": 29,
    "undefined_name": 14,
    "api_misuse": 14,
    "incomplete_generation": 7,
    "syntax_error": 2,
    "type_error": 2,
    "redundant_conditions": 1,
    "hallucinated_object": 1,
}

# Types that are harder to plant on a short next-line (higher → more retries/fallback).
PLANT_FAIL_RATE = {
    "misinterpretation": 0.05,
    "missing_corner_case": 0.12,
    "undefined_name": 0.18,
    "api_misuse": 0.15,
    "incomplete_generation": 0.22,
    "syntax_error": 0.28,
    "type_error": 0.25,
    "redundant_conditions": 0.30,
    "hallucinated_object": 0.35,
}

_FIT_WEIGHT = {3: 3.0, 2: 1.0}
_RECENT_PENALTY = 0.35
RECENT_WINDOW = 6


def _normalize(weights: Dict[str, float], keys: Sequence[str]) -> Dict[str, float]:
    w = {k: max(0.0, float(weights.get(k, 0.0))) for k in keys}
    s = sum(w.values())
    if s <= 0:
        return {k: 1.0 / len(keys) for k in keys}
    return {k: w[k] / s for k in keys}


def sample_fit_map(rng: random.Random) -> Dict[str, int]:
    """Synthetic selector ratings: often rate misinterpretation/corner-case as 3."""
    fits: Dict[str, int] = {}
    for sid in SKILLS:
        # Base: types that dominate logs get higher fit more often.
        bias = MODEL_PICK_WEIGHTS.get(sid, 1)
        roll = rng.random()
        if bias >= 25:
            fits[sid] = 3 if roll < 0.70 else (2 if roll < 0.92 else 1)
        elif bias >= 10:
            fits[sid] = 3 if roll < 0.35 else (2 if roll < 0.75 else (1 if roll < 0.92 else 0))
        elif bias >= 5:
            fits[sid] = 3 if roll < 0.15 else (2 if roll < 0.45 else (1 if roll < 0.75 else 0))
        else:
            fits[sid] = 3 if roll < 0.05 else (2 if roll < 0.25 else (1 if roll < 0.55 else 0))
    # Ensure at least one plantable type.
    if not any(fits[s] >= 2 for s in SKILLS):
        fits[rng.choice(SKILLS)] = 2
    return fits


def try_plant(rng: random.Random, bug_type: str) -> bool:
    return rng.random() >= PLANT_FAIL_RATE.get(bug_type, 0.2)


@dataclass
class SessionState:
    counts: Counter = field(default_factory=Counter)
    recent: List[str] = field(default_factory=list)
    deck: List[str] = field(default_factory=list)
    history: List[str] = field(default_factory=list)  # successful plants in order

    def record(self, bug_type: str) -> None:
        self.counts[bug_type] += 1
        self.recent = ([bug_type] + self.recent)[:RECENT_WINDOW]
        self.history.append(bug_type)


Selector = Callable[[SessionState, Dict[str, int], random.Random, int], List[str]]


def _weighted_order(
    skills: Sequence[str],
    weight_fn: Callable[[str], float],
    rng: random.Random,
) -> List[str]:
    pool = [(s, weight_fn(s)) for s in skills]
    pool = [(s, w) for s, w in pool if w > 0]
    order: List[str] = []
    while pool:
        s = rng.choices([x for x, _ in pool], weights=[w for _, w in pool])[0]
        order.append(s)
        pool = [(x, w) for x, w in pool if x != s]
    return order


def strategy_model_picks(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Current inline behavior: free model preference (empirical), ignore recent."""
    probs = _normalize(MODEL_PICK_WEIGHTS, SKILLS)
    # Soft-mask by fit: prefer types with fit>=2, but allow rare falls.
    masked = {
        s: probs[s] * (1.0 if fits.get(s, 0) >= 2 else 0.05)
        for s in SKILLS
    }
    order = _weighted_order(SKILLS, lambda s: masked[s], rng)
    return order[: max(need * 3, need)]


def strategy_fit_recent(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Agent-style: fit weight × recent penalty (current choose_bug_types draw)."""
    recent_counts = {t: state.recent.count(t) for t in set(state.recent)}

    def w(s: str) -> float:
        return _FIT_WEIGHT.get(fits.get(s, 0), 0.0) * (_RECENT_PENALTY ** recent_counts.get(s, 0))

    return _weighted_order(SKILLS, w, rng)


def strategy_fit_no_recent(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Same as fit_recent but ignore session history (toggle 'don't consider previous')."""

    def w(s: str) -> float:
        return _FIT_WEIGHT.get(fits.get(s, 0), 0.0)

    return _weighted_order(SKILLS, w, rng)


def strategy_uniform_among_fit(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Uniform among types rated fit>=2 (fallback: all skills)."""
    pool = [s for s in SKILLS if fits.get(s, 0) >= 2] or list(SKILLS)
    order = pool[:]
    rng.shuffle(order)
    # If need more candidates after failures, append rest shuffled.
    rest = [s for s in SKILLS if s not in order]
    rng.shuffle(rest)
    return order + rest


def strategy_least_used(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Prefer least-shown types; soft-prefer fit>=2, then allow any."""
    def key(s: str) -> Tuple[int, int, float]:
        # primary: count; secondary: prefer fit>=2; tertiary: random
        return (state.counts[s], 0 if fits.get(s, 0) >= 2 else 1, rng.random())

    return sorted(SKILLS, key=key)


def strategy_least_used_fit_only(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Least-used among fit>=2 first, then least-used among the rest."""
    good = [s for s in SKILLS if fits.get(s, 0) >= 2]
    bad = [s for s in SKILLS if s not in good]
    good.sort(key=lambda s: (state.counts[s], rng.random()))
    bad.sort(key=lambda s: (state.counts[s], rng.random()))
    return good + bad


def strategy_stratified_quota(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Pick type farthest below equal share of (shown so far + need)."""
    total_after = sum(state.counts.values()) + need
    target = total_after / len(SKILLS)

    def deficit(s: str) -> Tuple[float, int, float]:
        # Higher deficit first; prefer fit>=2 among ties.
        return (target - state.counts[s], 0 if fits.get(s, 0) >= 2 else 1, rng.random())

    return sorted(SKILLS, key=deficit, reverse=True)


def strategy_shuffle_deck(
    state: SessionState, fits: Dict[str, int], rng: random.Random, need: int
) -> List[str]:
    """Deal from a shuffled deck of all skills; reshuffle when empty. Soft-prefer fit≥2."""
    if not state.deck:
        state.deck = list(SKILLS)
        rng.shuffle(state.deck)
    good = [s for s in state.deck if fits.get(s, 0) >= 2]
    bad = [s for s in state.deck if fits.get(s, 0) < 2]
    # Successful plants remove from state.deck inside plant_with_retries.
    return good + bad


STRATEGIES: Dict[str, Selector] = {
    "model_picks (current inline)": strategy_model_picks,
    "fit×recent (current agent)": strategy_fit_recent,
    "fit only (no recent)": strategy_fit_no_recent,
    "uniform among fit≥2": strategy_uniform_among_fit,
    "least-used": strategy_least_used,
    "least-used among fit≥2": strategy_least_used_fit_only,
    "stratified quota": strategy_stratified_quota,
    "shuffle deck": strategy_shuffle_deck,
}


def plant_with_retries(
    state: SessionState,
    order: List[str],
    rng: random.Random,
    need: int,
    attempts: int = 2,
) -> List[str]:
    """Try types in order; on plant failure try next type. Up to `attempts` LLM-like tries."""
    planted: List[str] = []
    tried = 0
    for sid in order:
        if len(planted) >= need:
            break
        if sid in planted:
            continue
        tried += 1
        if try_plant(rng, sid):
            planted.append(sid)
            state.record(sid)
            if sid in state.deck:
                state.deck.remove(sid)
        # Match product BUG_ATTEMPTS: one suggestion gets at most `attempts` plant tries
        # when need==1; for multi-bug allow attempts per remaining slot.
        if need == 1 and tried >= attempts:
            break
        if need > 1 and tried >= attempts * need:
            break
    return planted


def shannon_entropy(counts: Counter, skills: Sequence[str] = SKILLS) -> float:
    total = sum(counts[s] for s in skills)
    if total <= 0:
        return 0.0
    h = 0.0
    for s in skills:
        p = counts[s] / total
        if p > 0:
            h -= p * math.log(p, 2)
    return h


def max_min_gap(counts: Counter, skills: Sequence[str] = SKILLS) -> int:
    vals = [counts[s] for s in skills]
    return max(vals) - min(vals)


def chi2_uniform(counts: Counter, skills: Sequence[str] = SKILLS) -> float:
    total = sum(counts[s] for s in skills)
    if total <= 0:
        return 0.0
    expected = total / len(skills)
    return sum((counts[s] - expected) ** 2 / expected for s in skills)


def run_session(
    strategy: Selector,
    steps: int,
    need: int,
    rng: random.Random,
    checkpoints: Sequence[int],
) -> Dict[int, Dict[str, float]]:
    state = SessionState()
    snap: Dict[int, Dict[str, float]] = {}
    for t in range(1, steps + 1):
        fits = sample_fit_map(rng)
        order = strategy(state, fits, rng, need)
        plant_with_retries(state, order, rng, need=need, attempts=2)
        if t in checkpoints:
            snap[t] = {
                "entropy": shannon_entropy(state.counts),
                "gap": float(max_min_gap(state.counts)),
                "chi2": chi2_uniform(state.counts),
                "shown": float(sum(state.counts.values())),
                "unique": float(sum(1 for s in SKILLS if state.counts[s] > 0)),
            }
            for s in SKILLS:
                snap[t][f"c:{s}"] = float(state.counts[s])
    return snap


def aggregate(
    sessions: List[Dict[int, Dict[str, float]]],
    checkpoints: Sequence[int],
) -> Dict[int, Dict[str, float]]:
    out: Dict[int, Dict[str, float]] = {}
    for t in checkpoints:
        keys = sessions[0][t].keys()
        out[t] = {}
        for k in keys:
            vals = [s[t][k] for s in sessions]
            out[t][k] = statistics.fmean(vals)
    return out


def print_report(
    name: str,
    agg: Dict[int, Dict[str, float]],
    checkpoints: Sequence[int],
) -> None:
    ideal_h = math.log(len(SKILLS), 2)
    print(f"\n=== {name} ===")
    print(f"{'t':>4}  {'entropy':>8}  {'gap':>5}  {'chi²':>8}  {'unique':>6}  top types")
    for t in checkpoints:
        m = agg[t]
        top = sorted(
            ((s, m[f"c:{s}"]) for s in SKILLS),
            key=lambda x: -x[1],
        )[:3]
        top_s = ", ".join(f"{s}={v:.1f}" for s, v in top)
        print(
            f"{t:4d}  {m['entropy']:8.3f}  {m['gap']:5.1f}  {m['chi2']:8.2f}  "
            f"{m['unique']:6.1f}  {top_s}"
        )
    print(f"      (ideal entropy = {ideal_h:.3f} for {len(SKILLS)} types)")


def score_strategy(agg: Dict[int, Dict[str, float]], checkpoints: Sequence[int]) -> float:
    """Lower is better: average chi² + gap, with late checkpoints weighted more."""
    score = 0.0
    weight_sum = 0.0
    for t in checkpoints:
        w = float(t)
        score += w * (agg[t]["chi2"] + agg[t]["gap"])
        weight_sum += w
    return score / weight_sum if weight_sum else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=40, help="Suggestions per simulated session")
    parser.add_argument("--sessions", type=int, default=200, help="Independent student sessions")
    parser.add_argument("--count", type=int, default=1, help="Buggy options per suggestion")
    parser.add_argument(
        "--checkpoints",
        default="5,10,20,40",
        help="Comma-separated suggestion counts to snapshot",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    checkpoints = [int(x) for x in args.checkpoints.split(",") if x.strip()]
    checkpoints = [t for t in checkpoints if 1 <= t <= args.steps]
    if args.steps not in checkpoints:
        checkpoints.append(args.steps)
    checkpoints = sorted(set(checkpoints))

    print(
        f"skills={len(SKILLS)}  sessions={args.sessions}  steps={args.steps}  "
        f"bug_options={args.count}  checkpoints={checkpoints}"
    )
    print(
        "Metrics: entropy↑ better (max "
        f"{math.log(len(SKILLS), 2):.3f}), gap↓ better, chi²↓ better (vs equal share)."
    )

    results: Dict[str, Dict[int, Dict[str, float]]] = {}
    for name, fn in STRATEGIES.items():
        snaps: List[Dict[int, Dict[str, float]]] = []
        for i in range(args.sessions):
            rng = random.Random(args.seed + i * 1009 + hash(name) % 10_000)
            snaps.append(run_session(fn, args.steps, args.count, rng, checkpoints))
        results[name] = aggregate(snaps, checkpoints)
        print_report(name, results[name], checkpoints)

    ranked = sorted(results.items(), key=lambda kv: score_strategy(kv[1], checkpoints))
    print("\n=== Ranking (lower score = more uniform over time) ===")
    for i, (name, agg) in enumerate(ranked, 1):
        last = agg[checkpoints[-1]]
        print(
            f"{i}. {name:32s}  score={score_strategy(agg, checkpoints):7.2f}  "
            f"at t={checkpoints[-1]}: H={last['entropy']:.3f} gap={last['gap']:.1f} "
            f"chi²={last['chi2']:.2f}"
        )

    best_name, best_agg = ranked[0]
    second_name, second_agg = ranked[1]
    print("\n=== Recommendation ===")
    print(f"Best overall: {best_name}")
    print(
        f"  At early t={checkpoints[0]}: gap={best_agg[checkpoints[0]]['gap']:.1f}, "
        f"H={best_agg[checkpoints[0]]['entropy']:.3f}"
    )
    print(
        f"  At late  t={checkpoints[-1]}: gap={best_agg[checkpoints[-1]]['gap']:.1f}, "
        f"H={best_agg[checkpoints[-1]]['entropy']:.3f}"
    )
    print(f"Runner-up: {second_name}")
    print(
        "For the instructor Uniform toggle, prefer a type-locked plant using the winning "
        "policy (not free model_picks). Soft-filter with fit≥2 when possible so forced "
        "types still look realistic."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
