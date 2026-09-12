# Second-checkpoint change pilots

These are follow-up benchmark tasks, not completed candidate solutions. Each starts
from a **completed first-checkpoint submission** in the assigned language. The
original pre-extension starters are not qualified starting points for checkpoint
two: agents must first implement NULL/outer joins or durable workflow recovery.
The ledger calibration did not produce those predecessor implementations.

| Existing task | Second change | Public regression / new | Private regression / new |
| --- | --- | --- | --- |
| [Query engine](query-null/PROBLEM.md) | Incremental views, atomic multi-table updates, retractions, joins and aggregates | 55 / 20 | 28 / 52 |
| [Workflow runner](workflow-recovery/PROBLEM.md) | Multiple workers, leases, fenced delivery, cancellation and failure draining | 57 / 11 | 26 / 31 |

There are **280 cases: 166 unchanged predecessor regressions and 114 new cases**.
Query traces are paired across optimizer settings. Private additions include
self-joins, chained outer joins, three-valued filters, duplicate extrema, stale
transients, repeated cancellation during lookup, and long seeded interleavings.
Case counts are not independent trials or a statistical measure of coverage.

The [SlopCodeBench design philosophy](https://www.scbench.ai/design-philosophy)
motivates revealing changes sequentially so later work exposes the consequences
of earlier design decisions. We also examined its
[DAG execution](https://www.scbench.ai/problems/dag_execution) and
[file query](https://www.scbench.ai/problems/file_query_tool) problem structures.
These specifications and fixtures are original to this repository. We retain
public tests and the current budgets; we have not adopted its tests-hidden policy.

## Staging and comparisons

1. **Checkpoint one:** use the existing query-null or workflow-recovery task,
   original language-specific starter, and original public bundle. Do not expose
   this directory, second-checkpoint prompts, private data, or authoring context.
2. **Freeze:** capture source, diff, time, tool/usage records and public/held-out
   results using the existing evaluator. Do not feed private results back to the
   implementation agent. Record human review separately.
3. **Checkpoint two, chained cohort:** give a fresh session the frozen source from
   that same model/language/rollout and only the new public bundle. Transfer source,
   not transcripts, hidden grades, identities of other models, or other languages.
   Carry every finished non-infrastructure submission forward, including incorrect
   ones, so selection does not hide first-stage failures. Do not repair between
   stages. Record the parent run and source hash in evaluator-only lineage data.
4. **Checkpoint two, controlled cohort:** separately choose and audit one passing
   first-stage implementation per language *before* measuring follow-up models.
   All models in that language receive that exact source. Freeze these choices;
   do not select a different favorable predecessor for each model. Functional
   parity does not itself guarantee comparable architecture across languages.
5. Grade old and new behaviors, public and held-out, separately. Report chained
   success over all stage-one starts, checkpoint-two success conditional on a
   passing predecessor, and the controlled cohort separately. Preserve infrastructure
   failures as infrastructure outcomes. Never compare these cohorts as interchangeable.

Keep the existing 30-minute / 100-tool-call policy per checkpoint; native-client
turn caps and accounting caveats remain as documented in `../experiments/NATIVE.md`.
No tighter budgets or model calls are introduced by this package. Native clients
retain different system prompts and token accounting. Save settings per stage.

Human review should record stage-one and stage-two times/decisions separately.
Review stage-two diffs against their actual predecessor. Use the new review
questions at the end of each problem; do not reuse the earlier comprehension
question unchanged. Useful quantitative records include new test success,
regressions introduced, agent time, tool calls, patch size, and proportion of the
preceding source rewritten. Patch size remains a proxy, not a quality verdict.

## Prepare a public bundle

A tests-only export needs no completed implementation:

```sh
python3 evals/change-pilots/followups/export_public.py --task query-null \
  --output /path/outside/repository/query-checkpoint-two
```

After checkpoint one, export its frozen source too:

```sh
python3 evals/change-pilots/followups/export_public.py --task workflow-recovery \
  --predecessor-run /path/to/results/runs/RUN_ID --cohort chain \
  --output /path/to/agent-bundles/workflow-stage-two \
  --receipt /path/to/evaluator-only/workflow-stage-two-lineage.json
```

The exporter verifies the task/language, frozen source hash and the original
public/held-out grading corpus hashes. Controlled mode additionally requires a
passing predecessor. It copies only public specification/test/runner files and
that predecessor's source, preserving executable flags. The public manifest has
file/source hashes; the separate private receipt has lineage and predecessor
outcome. Exporting performs no model calls or submitted-code execution.

Neither an exporter nor a directory name enforces isolation. Mount only the
resulting bundle into a fresh agent environment with the assigned toolchain; deny
access to this checkout, its history, private receipt, other sources and results.
All `evaluator/` files and `tests/` files are evaluator-only, including generators
and private reference models. A fresh measured session must not inherit this
benchmark-authoring conversation.

## Validate and grade

```sh
python3 evals/change-pilots/followups/run.py validate
python3 evals/change-pilots/followups/run.py validate \
  --corpus-root evals/change-pilots/followups/evaluator/heldout
python3 -m unittest discover -s evals/change-pilots/followups/tests -v
python3 evals/change-pilots/followups/evaluator/freeze.py
```

`run.py` reuses the original language-neutral process driver, its strict JSON
comparison, time/output limits and report format. It never implicitly loads private
cases. In each exported bundle, use `python3 run.py run --task TASK --command
'./starter/run.sh'`. This direct runner is for trusted launchers and is not a sandbox.

For untrusted frozen submissions, the dedicated evaluator builds in isolation and
runs each case in a fresh network-disabled Docker container, supplying only the
request over stdin, never mounting expected outputs or evaluator code:

```sh
python3 evals/change-pilots/followups/evaluator/grade.py --task query-null \
  --language typescript --source /path/to/frozen/source \
  --image sha256:YOUR_PINNED_TASK_IMAGE --report /path/to/results/stage-two.json
```

Compile Prism through its existing build.sh; other launchers run source as before.
The report records source, image and evaluator manifest hashes, per-corpus/per-phase
scores, and errors. Partial phase selections are explicitly labeled. Algorithmic
qualification is separate from behavioral acceptance. Reports must be new and
outside Git. The old native batch planner still schedules checkpoint-one tasks;
**automatic two-stage native scheduling is not wired into it by this package**.
The exporter and standalone grader are the preparation/grading interfaces for the
follow-up controller. No new model rollouts have been run or scheduled here.

## Scaling and oracle limitations

The incremental-query task forbids full recomputation as its production strategy.
The correctness corpus alone cannot prove that requirement. Source review and
scaling measurements are required before calling an implementation qualified:

```sh
python3 evals/change-pilots/followups/evaluator/scaling.py \
  --command './path/to/trusted-or-isolated-launcher' \
  --report /path/to/results/query-scaling.json
```

This measures point updates and updates to an unrelated table at increasing table
sizes, checking every answer. It retains warmup and repeated measurements, reports
medians/growth, and includes initial construction/process startup. These costs may
mask incremental maintenance costs, so use profiles and source review together.
Performance acceptance is deliberately null until an efficient implementation and
a recomputation control establish fair toolchain/machine-specific thresholds.
It introduces no hidden time requirement or tighter agent budget. Run scaling
separately from timed model runs to avoid resource contention. The process launcher
must itself provide isolation when measuring untrusted code.

The private workflow model is a small executable transition specification. The
private query oracle recomputes selected valid SQL with SQLite; it is deliberately
not an eligible incremental solution, nor an oracle for the whole previous static
type/error contract. Existing fixed regression expectations are retained verbatim.
New expectations receive hand-computed anchors, an independent Python relational
cross-check for every generated query trace, workflow audit/state invariants, and
two explicit public-surviving/private-killed fault witnesses. These checks improve
confidence; they are not a proof or evidence that measured models will fail.
Completed Prism/Python/TypeScript implementations of these extensions have not yet
been evaluated. The fixture models never enter an agent bundle.

To deliberately revise the suite, run `evaluator/generate.py`, review the data and
coverage changes, rerun tests, and refresh `evaluator/freeze.py --write`. Freeze a
new experiment revision before implementation runs. Do not revise hidden tests in
response to scored submissions and then treat the same submissions as independent
measurements on an unseen test set. The original pilot corpus/manifests stay intact.
