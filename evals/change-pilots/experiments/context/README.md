# Language-context experiment

The original calibration supplied the starter implementation and paths to offline
Prism documentation, but no language primer in the initial prompt. Of its 32 Prism
stages, 25 recorded execute commands containing `/opt/prism-docs`, 20 mentioned
`/opt/prism-docs/spec.md`, and 22 mentioned `/opt/prism-lib`. These are trace-string
counts, not proof of what a model read or understood.

`prism-0.18.md` is a short, task-independent briefing about syntax, standard-library
APIs, collection costs, immutable state, and the build/test loop. Its complete code
examples are checked with Prism 0.18.0; the opt-in test accepts the pinned task
image or an explicitly supplied 0.18.0 release binary. It includes no held-out
cases, test answers, performance thresholds, or query/workflow solution algorithm.

For a new chained plan, add `--prism-briefing`:

```sh
PYTHONPATH=evals/change-pilots python3 -m experiments.chained_batch plan \
  --results /path/to/new-prism-briefing-cohort \
  --language prism --model luna --model terra \
  --prism-briefing \
  --performance-calibration /path/to/existing/calibration.json
```

Planning does not run any models. Normal `chained_batch run` starts the resulting
plan. Without the flag, prompt text remains unchanged. With it, the briefing is
prepended directly to both Prism checkpoint prompts; other languages retain their
original prompts. Every checkpoint starts a fresh native session, so each needs
its own briefing. Both full prompts, a `prism-brief-v1` profile label and the
briefing hash are frozen in the plan. Each run metadata record carries its profile.
Changes to the briefing invalidate a planned experiment instead of silently
changing context partway through it.

Compare a new baseline cohort against a new briefed cohort with the same models,
tasks, starting sources, toolchain, effort, budgets and performance calibration.
Use repetitions and counterbalance/interleave the cohorts where possible. The old
cohort is useful preliminary context, but one fresh briefed rollout versus one old
baseline rollout cannot isolate the effect from stochastic/provider/time variation.
Compare functional acceptance, compilation/resource flags, time/tool calls, and
performance screens separately. A successful test would measure the benefit of
onboarding; it would not prove that every prior Prism failure came from unfamiliarity.

Verify the examples without model inference:

```sh
PRISM_EVAL_DOCKER_TESTS=1 python3 -m unittest discover \
  -s evals/change-pilots/tests -p test_prism_briefing.py -v
```

Alternatively, set `PRISM_BRIEFING_COMPILER=/absolute/path/to/prism-0.18.0` when
running that test. It refuses a different compiler version. The installed host
compiler may have advanced since the original evaluation; do not validate this
briefing against its unpinned `prism` command. `VERIFICATION.json` records the
release binary and outputs used for this briefing's initial verification.
