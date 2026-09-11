"""Published-price scenarios, not empirical predictions or authorization to spend."""

import json
from pathlib import Path

CATALOG = Path(__file__).with_name("models.json")
SCENARIOS = {
    "light": {"input": 150_000, "output": 15_000},
    "planning": {"input": 600_000, "output": 60_000},
    "heavy": {"input": 2_000_000, "output": 200_000},
}


def catalog():
    return json.loads(CATALOG.read_text())


def select_models(keys=None):
    models = catalog()["models"]
    if keys is None:
        return models
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("select at least one model, without duplicates")
    indexed = {model["key"]: model for model in models}
    if set(keys) - indexed.keys():
        raise ValueError("unknown model key")
    return [indexed[key] for key in keys]


def estimate(models, tasks=3, languages=3, repetitions=3):
    if any(type(n) is not int or n < 1 for n in (tasks, languages, repetitions)):
        raise ValueError("matrix dimensions must be positive integers")
    count = tasks * languages * repetitions
    rows = []
    for model in models:
        prices = model["pricing"]
        per_run = {name: (tokens["input"] * prices["input"] +
                          tokens["output"] * prices["output"]) / 1_000_000
                   for name, tokens in SCENARIOS.items()}
        rows.append({"model": model["key"], "model_id": model["model_id"],
                     "runs": count, "per_run_usd": per_run,
                     "total_usd": {name: round(cost * count, 4) for name, cost in per_run.items()}})
    return {"kind": "unmeasured_scenario", "verified_on": catalog()["verified_on"],
            "runs": len(models) * count, "scenarios_tokens_per_run": SCENARIOS,
            "assumptions": [
                "Input is cumulative billed input across every agent request, including replayed history.",
                "Output includes billed reasoning/thinking tokens, not just code.",
                "No caching or batch discounts; no explicit cache writes; standard service tier.",
                "Each OpenAI request stays below the long-context pricing threshold.",
                "Identical token scenarios across models/languages are assumptions, not observed usage.",
                "Excludes subscription charges, compute, taxes, paid retries, and human review.",
                "Published prices and account access must be rechecked before later experiments."],
            "models": rows,
            "total_usd": {name: round(sum(row["total_usd"][name] for row in rows), 4)
                          for name in SCENARIOS}}


def format_estimate(report):
    lines = [f"{report['runs']} runs; unmeasured API cost scenarios (USD)",
             "Model          Runs      Light   Planning      Heavy"]
    for row in report["models"]:
        c = row["total_usd"]
        lines.append(f"{row['model']:<14} {row['runs']:>4} {c['light']:>10.2f} {c['planning']:>10.2f} {c['heavy']:>10.2f}")
    c = report["total_usd"]
    lines.append(f"{'TOTAL':<14} {report['runs']:>4} {c['light']:>10.2f} {c['planning']:>10.2f} {c['heavy']:>10.2f}")
    return "\n".join(lines)
