# Prism 0.22 runtime and onboarding validation

The current runtime pins upstream Prism 0.22.0 at
`b643c4acfd371ba37d13fdedf0742154e5b69806`. The images were built locally on
2026-09-13. Python 3.14.7, Node 25.2.1, TypeScript 5.9.3, Codex 0.154.0, and
Claude Code 2.1.257 keep their existing pins. No scored rollouts or real provider
inference requests were made during migration.

| Image | Immutable ID |
| --- | --- |
| `prism-change-pilots:0.22.0-py3.14.7-node25.2.1` | `sha256:89b6809abb2e4a54bcba2d4d718ea38473bdca5d6e8e7019b763432df3337ec6` |
| `prism-native-clients:prism0.22.0-codex0.154.0-claude2.1.257` | `sha256:12f4e9145b5ce9e168c5088d43eb1b7ff416bbc7a486de19ea73e1cdb3b2d623` |
| `prism-change-pilots:0.22.0-native-tools-v2` | `sha256:6e48c61358a291af0027ee2522778127f908178a9c6a850db96889850c206770` |

Source and language versions are pinned; mutable base-image tags and apt packages
can produce different image IDs when rebuilt. Plans freeze the actual IDs and
require matching verification and calibration receipts.

## Compatibility changes

- Added the new compiler-embedded `packages/lint` inputs to the allowlisted build
  context. The context still excludes evaluation files, worktrees, and Git metadata.
- Added the matching tutorial, all eight chapters, compiler reference, and tutorial
  project examples to the offline documentation in each runtime image.
- Added required package version, authors, maintainers, and license fields to the
  ledger starter and Prism performance-control manifests. The other starters build
  directly from source and do not need a manifest change.
- Raised the shared task/grading file-descriptor cap from 256 to 1,024: the 0.22
  linker otherwise fails to build the query starter with “Too many open files.”
  CPU, memory, wall-time, and tool-call budgets are unchanged.
- Rebuilt the native task image from newly resolved immutable compiler/client
  inputs and refreshed offline verification for every configured model/effort pair.

## Acceptance and onboarding evidence

[Runtime verification](RUNTIME_VERIFICATION-0.22.json) records the compiler source
archive and all nine starter checks in isolated containers: **192 public + 45
held-out baseline cases = 237/237 passed**. Each starter also returns a valid response
that fails its positive extension witness, so the requested extension remains work
for the model. The three Prism starters additionally passed their baseline checks
with the installed macOS 0.22 compiler. Acceptance cases and problem contracts did
not change.

[Native verification](NATIVE_VERIFICATION.json) covers all eight offline model/effort
routes and both provider boundaries using fresh blank authentication volumes and
local fake-provider fixtures. Actual task-tool execution is checked. It proves
routing/isolation, not subscription access, live model availability, or performance.

[The onboarding document](context/prism-0.22.md) is approximately 9,700 words and
66 KB. `--prism-briefing` prepends its full quick reference and tutorial to both
Prism checkpoints under profile `prism-tutorial-v2`. Other languages and the
unbriefed condition keep their original prompts. The 0.18 briefing is removed.
[Source hashes](context/TUTORIAL_SOURCES.json) pin all nine tutorial files and
record rendering changes. [Example evidence](context/VERIFICATION-0.22.json)
records, on both macOS and Linux, 31 native programs with checked output, three
additional successful typechecks, and six expected compile failures. Two explicitly
marked project-dependent/typed-hole fragments are excluded, not counted as passes.

The full harness suite passed **189/189 with Docker enabled and no skips**.
Follow-up maintenance passed 35 tests, held-out maintenance passed nine, and
analysis/report tests passed four.

## Fresh performance calibration and planning

[The new calibration](PERFORMANCE_CALIBRATION-0.22.json) uses the pinned 0.22 task
image, 9,999 rows per joined table, 999 updates, one warmup and three measured
repetitions. All incremental/recompute controls returned correct results; all six
language/profile combinations separated enough to produce cutoffs under the
existing 1.5x-margin rule. These are per-language screening thresholds for this
machine/image, not a general cross-language speed comparison.

| Language | Profile | Screening cutoff (seconds) |
| --- | --- | ---: |
| python | point-updates | 0.400 |
| python | unrelated-table | 0.393 |
| typescript | point-updates | 0.405 |
| typescript | unrelated-table | 0.421 |
| prism | point-updates | 2.270 |
| prism | unrelated-table | 2.283 |

A fresh 12-cell planning smoke check passed against the new image and calibration.
All four Prism cells (both checkpoints of both problems) contained the complete
frozen context; all eight Python/TypeScript prompts matched the unbriefed condition.
A wrong-image calibration was rejected. These were temporary plans with no model
execution. Use the new calibration file with `--performance-calibration` and add
`--prism-briefing` for the tutorial condition.

## Reproduce the migration checks

From the repository root:

```sh
python3 evals/change-pilots/experiments/build_image.py
PYTHONPATH=evals/change-pilots python3 -m experiments.native_setup build
PYTHONPATH=evals/change-pilots python3 -m experiments.native_task_setup
PYTHONPATH=evals/change-pilots python3 -m experiments.native_check verify-all \
  --output evals/change-pilots/experiments/NATIVE_VERIFICATION.json
PYTHONPATH=evals/change-pilots python3 -m experiments.verify_starters \
  --output /path/to/new-starter-verification.json
PRISM_EVAL_DOCKER_TESTS=1 python3 -m unittest discover \
  -s evals/change-pilots/tests -v
PYTHONPATH=evals/change-pilots python3 -m followups.evaluator.performance calibrate \
  --image prism-change-pilots:0.22.0-native-tools-v2 \
  --output /path/to/new-0.22-performance-calibration.json
```

Performance calibration must run without other evaluation builds/tests competing
for resources. Keep old results, image identities, and calibration receipts as
historical evidence. Start a fresh plan for 0.22 and compare onboarding conditions
on the same runtime; a direct old-0.18/new-0.22 comparison changes two variables.
