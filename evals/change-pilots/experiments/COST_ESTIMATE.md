# API cost scenarios — 2026-09-11

These are arithmetic scenarios using published prices, **not measured rollout costs**.
No authenticated model requests were made to produce this estimate. Model access still
needs to be checked against the intended API accounts.

All four OpenAI classes are Luna, Terra, Sol and Astra. All four Anthropic classes
are Haiku, Sonnet, Opus and Fable. These class labels are experimental groupings,
not claims of equal capability across providers.

## Full pilot: 216 independent rollouts

Eight models × three tasks × three languages × three repetitions = 216 runs,
27 runs per model. The planning scenario assumes **600,000 cumulative input tokens
and 60,000 output tokens per rollout**, including replayed history and billed thinking.

| Model | Input / output USD per million | Planning USD per rollout | Planning USD for 27 runs |
| --- | ---: | ---: | ---: |
| [gpt-5.6-luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | $0.2 / $1.2 | $0.192 | $5.18 |
| [gpt-5.6-terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | $2 / $12 | $1.920 | $51.84 |
| [gpt-5.6-sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | $4 / $20 | $3.600 | $97.20 |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | $10 / $50 | $9.000 | $243.00 |
| [claude-haiku-4-5-20251001](https://platform.claude.com/docs/en/about-claude/pricing) | $1 / $5 | $0.900 | $24.30 |
| [claude-sonnet-5](https://platform.claude.com/docs/en/about-claude/pricing) | $2 / $10 | $1.800 | $48.60 |
| [claude-opus-5](https://platform.claude.com/docs/en/about-claude/pricing) | $5 / $25 | $4.500 | $121.50 |
| [claude-fable-5-1](https://platform.claude.com/docs/en/about-claude/pricing) | $10 / $50 | $9.000 | $243.00 |

**Planning total: $834.62.**

| Scenario | Cumulative input / output per rollout | 216-run total |
| --- | ---: | ---: |
| Light | 150,000 / 15,000 | $208.66 |
| Planning | 600,000 / 60,000 | $834.62 |
| Heavy | 2,000,000 / 200,000 | $2,782.08 |

The light/heavy range is a sensitivity analysis, not a confidence interval or a
guaranteed cap. These estimates assume no caching discounts or explicit cache writes,
standard service, and individual OpenAI requests below 272,000 input tokens. Cumulative
input across the whole rollout can exceed that threshold without any individual
request doing so. Models and languages will consume different amounts in practice.

## Smaller first steps

| Experiment | Runs | Planning scenario |
| --- | ---: | ---: |
| One task/language, all eight models | 8 | $30.91 |
| One task, all languages/models | 24 | $92.74 |
| All tasks/languages/models once | 72 | $278.21 |
| Full matrix with five repetitions | 360 | $1,391.04 |

Start with the 24-run, one-task calibration if API costs are acceptable. Inspect actual
token usage, stopping reasons and time distributions before committing to the full
216-run pilot. Calibration is exploratory; freeze settings for the subsequent scored
pilot and keep its runs separate. A nominal **$150 cumulative calibration allowance**
would cover the planning scenario, but the controller may stop earlier if its whole-run
reservation does not fit. This is a proposed budget, not spending authorization.

## Subscription alternative

Existing ChatGPT Pro / Claude Max subscriptions may cover native CLI runs within
their included allocations, with no additional model charge. They do not pay for
requests from the custom API runner. Limits, model access and any optional paid
usage must be checked; no unlimited/free-throughput assumption is used here.
[Codex authentication](https://learn.chatgpt.com/docs/auth) and
[Claude Code subscription usage](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan).

Both installed host clients reported subscription login during setup. The isolated
Codex client recognizes ChatGPT login; isolated Claude now recognizes Max login.
[Native setup](NATIVE.md) supplies whole-client containers and real offline routing
checks. The native batch scheduler records its own subscription experiment, because its harness
differs from the shared API loop. Native CLI dollar estimates are API-equivalent
figures, not subscription invoices.

## Reproduce or update

```sh
python3 evals/change-pilots/experiments/control.py estimate
python3 evals/change-pilots/experiments/control.py estimate --repetitions 5
python3 evals/change-pilots/experiments/control.py estimate --task ledger-refunds --repetitions 1
```

Prices are frozen in `models.json` with supporting official pages. In particular, Sol
currently has promotional pricing, and the Anthropic pricing page states Sonnet 5’s
$2/$10 rates are now standard. Recheck prices for later campaigns; do not silently
change prices/settings in an already frozen experiment. Infrastructure, taxes and
human review time are excluded from all API totals.
