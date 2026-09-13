# Prism onboarding context

[`prism-0.22.md`](prism-0.22.md) is the complete agent onboarding document: roughly
9,700 words / 66 KB, consisting of a practical evaluation quick reference followed
by the upstream **Taste the Rainbow** tutorial and all eight chapters. It covers
functions, algebraic data types, effect rows and row polymorphism, handlers,
continuations and resumption grades, coeffects, lenses, streams, modules, testing,
and content identity. The quick reference adds version-specific syntax, collection
costs, scoped mutation, error representation, and the evaluation build/tool boundary.

This is task-independent language education. It includes no held-out cases,
acceptance answers, performance thresholds, or query/workflow solution algorithm.
Tutorial setup commands are explicitly marked as background: agents use the
installed offline environment and extend the existing starter.

The compiler, offline spec/library/compiler reference, tutorial, and tutorial
project examples come from the same pinned 0.22 commit. Original tutorial Markdown
is installed at `/opt/prism-docs/tutorial.md` and `/opt/prism-docs/tutorial/`.
The 0.18 briefing has been removed. Previous plans/results retain their original
frozen prompts and provenance; use a fresh plan for the new runtime/context.

## Preload it into both checkpoints

```sh
PYTHONPATH=evals/change-pilots python3 -m experiments.chained_batch plan \
  --results /path/to/new-prism-tutorial-cohort \
  --language prism --model luna --model terra \
  --prism-briefing \
  --performance-calibration /path/to/0.22-calibration.json
```

Planning does not run models. The flag prepends the **entire document**, including
the tutorial, to both Prism checkpoint prompts. Each checkpoint starts a fresh
native session. Other languages keep their original prompts; omitting the flag
keeps the unbriefed baseline condition. Full prompts, the `prism-tutorial-v2` profile,
and the document SHA-256 are frozen in the plan and recorded with the run. Editing
any part of the document invalidates a pending plan instead of changing its context
mid-experiment. The native planner also requires fresh matching image/routing
verification; a 0.18 performance receipt is rejected for the new image.

Compare newly run baseline and briefed cohorts on **the same 0.22 runtime**, with
matching models, tasks, starting sources, effort, budgets, and calibration. Use
repetitions and interleave/counterbalance cohorts where possible. Comparing old
0.18 results directly to briefed 0.22 results mixes compiler and onboarding effects.
The larger prompt has its own token/time cost; report that along with acceptance,
compilation/resource flags, tool calls, wall time, and separate performance screens.

## Keep the tutorial and examples verifiable

The tutorial is frozen into the briefing from the pinned Git commit, not fetched
from a changing website. `TUTORIAL_SOURCES.json` records each chapter's source hash
and rendering changes: expand book-only hidden scaffolding, remove tab directives,
resolve links to offline paths, and use native output for the string-list example.
To regenerate or check only the tutorial portion:

```sh
PYTHONPATH=evals/change-pilots python3 -m experiments.context.update_tutorial
PYTHONPATH=evals/change-pilots python3 -m experiments.context.update_tutorial --check
```

`verify_briefing.py` typechecks ordinary snippets, requires `compile_fail` snippets
to fail, natively compiles/runs complete programs, and compares their printed output.
Only the two explicitly marked `ignore` fragments (a project import and a typed hole)
are excluded; they are retained and explained rather than counted as passing tests.

```sh
PRISM_BRIEFING_COMPILER=/absolute/path/to/prism python3 -m unittest discover \
  -s evals/change-pilots/tests -p test_prism_briefing.py -v
PRISM_EVAL_DOCKER_TESTS=1 python3 -m unittest discover \
  -s evals/change-pilots/tests -p test_prism_briefing.py -v
```

Both modes require exactly Prism 0.22.0. Docker mode uses the new task image and
mounts only the public briefing and verifier. `VERIFICATION-0.22.json` contains
compiler identities, the full briefing hash, and per-example evidence. These
checks make no provider inference requests.
