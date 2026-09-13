# Language-context experiment

The original calibration supplied the starter implementation and paths to offline
Prism documentation, but no language primer in the initial prompt. Of its 32 Prism
stages, 25 recorded execute commands containing `/opt/prism-docs`, 20 mentioned
`/opt/prism-docs/spec.md`, and 22 mentioned `/opt/prism-lib`. These are trace-string
counts, not proof of what a model read or understood.

The task-independent briefings cover syntax, standard-library APIs, collection
costs, state, and the build/test loop. They include no held-out cases, test answers,
performance thresholds, or query/workflow solution algorithm.

- [`prism-0.22.md`](prism-0.22.md) is the current upstream edition, checked against
  Prism 0.22.0. It adds typed `var` bindings and field-path assignments, explains
  the inferred ordering brand on `Map`, and expands collection-cost guidance using
  the new explicit complexity documentation. Local typed `let` bindings remain
  unsupported. Its three complete examples are compiled and executed.
- [`prism-0.18.md`](prism-0.18.md) is preserved for the existing 0.18.0 task images
  and `prism-brief-v1` profile. Its original verification receipt and text remain
  unchanged so archived experiments retain their documented language context.

**The evaluation runtime is still pinned to 0.18.0.** `--prism-briefing` therefore
continues to inject the 0.18 edition. Rebasing the repository does not update the
Docker images, their offline documentation, native-client verification receipts,
or performance calibration. Before using the 0.22 briefing in a cohort, rebuild
and verify the task environment on 0.22, check the starters, recalibrate performance,
and wire the new edition into a distinct frozen context profile. Do not inject the
0.22 briefing into an existing 0.18 plan or compare a combined compiler/briefing
upgrade as if it isolated the briefing's effect.

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

Alternatively, supply a release binary explicitly:

```sh
PRISM_BRIEFING_COMPILER=/absolute/path/to/prism python3 -m unittest discover \
  -s evals/change-pilots/tests -p test_prism_briefing.py -v
```

The test reads `--version` and selects the matching 0.18.0 or 0.22.0 edition;
other versions are rejected. The Docker mode always checks the 0.18 edition with
the pinned image. `VERIFICATION.json` records the original 0.18 release-binary
verification; `VERIFICATION-0.22.json` records the 0.22 compiler and example outputs.
