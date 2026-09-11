# Baseline starter programs

Each pilot has three independent source implementations: **Prism, Python, and
TypeScript**. Python and TypeScript are used consistently across tasks so the
comparison language does not vary with the problem domain. The SQL engines
implement their own parser, binder, planner, optimizer, and evaluator; they do
not delegate query execution to SQLite or another SQL engine.

These are the starting programs for a modification evaluation. They implement
the published **baseline** contract; the requested extension remains unimplemented.
They are not completed reference solutions or scored evaluation runs. Starter
construction can coordinate architecture across variants; the later measured
extension runs must use fresh sessions and single-language exports.

| Pilot | Prism | Python | TypeScript | Baseline responsibility |
| --- | --- | --- | --- | --- |
| Query engine | [source](query-null/prism/) | [source](query-null/python/) | [source](query-null/typescript/) | Non-null SQL, inner joins, grouping, stable ordering, and real optimization |
| Workflow runner | [source](workflow-recovery/prism/) | [source](workflow-recovery/python/) | [source](workflow-recovery/typescript/) | In-memory DAG scheduling, successful service calls, and immutable observations |
| Invoice ledger | [source](ledger-refunds/prism/) | [source](ledger-refunds/python/) | [source](ledger-refunds/typescript/) | Invoices, allocated payments, close periods, idempotency, and historical projections |

Every directory has a `run.sh`, a README, and `starter.json` listing exactly the
source/configuration files that belong in an export. Prism also has `build.sh`;
compile once before running acceptance cases. Build outputs and node_modules
are gitignored and excluded from the manifests. Each program runs its domain
logic in the named language; launchers only select a runtime or native executable.

## Starter acceptance

The existing fixture `phase` field already partitions the tests. No public or
held-out cases need to be moved, relabeled, or weakened:

| Pilot | Public baseline | Evaluator-only baseline |
| --- | ---: | ---: |
| Query engine | 21 | 5 |
| Workflow runner | 19 | 5 |
| Invoice ledger | 24 | 5 |

A starter must pass the baseline cases. A completed modification must pass both
baseline and extension cases. Some extension error cases can already pass on a
starter; that does not mean the extension is implemented. The optional
`--verify-incomplete` check requires a well-formed response that fails one positive
public extension scenario per task. A crash or malformed response does not count
as a successful incompleteness check.

From the repository root:

```sh
# Build once where required, then run all nine variants against public baseline cases.
python3 evals/change-pilots/starters/check.py --build --verify-incomplete \
  --report evals/change-pilots/reports/starters-public.json

# Select one variant.
python3 evals/change-pilots/starters/check.py --task query-null --language prism --build

# Evaluator-only: verify both public and held-out baseline cases.
python3 evals/change-pilots/starters/check.py --corpus both --verify-incomplete \
  --report evals/change-pilots/reports/starters-complete-baseline.json

# Also prove that fresh exports build/run without sibling implementations or build artifacts.
python3 evals/change-pilots/starters/check.py --exported --build --corpus both --verify-incomplete \
  --report evals/change-pilots/reports/starter-exports.json
```

Reports fingerprint each starter and each selected corpus. The standard runner
also works directly with any `run.sh`, using `--phase baseline`. TypeScript
projects include pinned development dependencies, a lockfile, and a strict
`npm run typecheck`; Node runs the erasable TypeScript source directly, so npm
dependencies are for checking rather than execution. See each language README
for toolchain and build instructions.

The tested toolchain is Prism 0.18.0, Python 3.14.7, Node 25.2.1, and TypeScript
5.9.3 with `@types/node` 25.0.3. The TypeScript adapters use Node's JSON reviver
source context to preserve the distinction between integer and floating-point
tokens. Use the tested Node version rather than a runtime without that facility.

## Export for a measured run

```sh
python3 evals/change-pilots/export_public.py --task query-null --language prism \
  --output /path/outside/this/repository/query-prism
```

The bundle contains `starter/` for exactly the selected language, public specs
and cases, and the runner. It has neither other implementations nor Git history,
private tests, validation reports, or these authoring notes. Source script
executable bits and content hashes are preserved. Do not reuse the starter-authoring
agent sessions for measured extension runs, and do not mount this evaluator
checkout in those runs. The exporter assembles inputs; the evaluation environment
must enforce filesystem/network isolation.
