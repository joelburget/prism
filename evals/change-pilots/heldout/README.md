# Evaluator-only held-out corpus

These 77 cases are additional, fixed scenarios under the already published contracts.
No cases were removed from the public suite. Each task has a `COVERAGE.md` explaining
what every held-out scenario adds and a plausible faulty implementation it would
catch. Those are reasoned coverage hypotheses, not claims of measured mutation kills.

| Task | Baseline / extension | Fixtures | Coverage rationale |
| --- | --- | --- | --- |
| Query engine | 5 / 23 | [cases](query-null/cases.json) | [new query interactions and binder boundaries](query-null/COVERAGE.md) |
| Workflow runner | 5 / 21 | [cases](workflow-recovery/cases.json) | [new failure/recovery traces](workflow-recovery/COVERAGE.md) |
| Invoice ledger | 5 / 18 | [cases](ledger-refunds/cases.json) | [new accounting and correction sequences](ledger-refunds/COVERAGE.md) |

`MANIFEST.json` records counts and hashes for both corpora, the public problem
descriptions, and the runner. Treat those inputs as frozen for an experiment.
There is no completed task implementation here, so these cases have not yet been
run against actual Prism or comparison-language submissions.

## Isolation and separate runs

This directory and its committed Git history are **evaluator material**. A folder
named `heldout` is not an access-control boundary. Do not give a measured agent
this source checkout, a clone containing its history, these coverage notes, private
reports, or the conversations used to author/review these cases. Keep this commit
and private cases out of repositories or services the measured agents can access.

Use `../export_public.py` to create an allowlisted task bundle. Add only the assigned
language's starter and tooling to that run. Put the bundle in an isolated evaluation
environment without access to this evaluator checkout or the other language's run.
Fresh contexts are required: agents that authored these cases are not eligible to
serve as independent implementation agents for the same held-out evaluation.

The exporter provides a clean input artifact, not a filesystem/network sandbox.
The evaluator should run submitted programs in isolation too: supply each request
over the wire, without mounting this corpus in the submitted program's environment.
The same JSON process protocol and test data apply independently to both languages.

## Validate and evaluate

From the repository root:

```sh
python3 evals/change-pilots/run.py validate --corpus-root evals/change-pilots/heldout
python3 evals/change-pilots/heldout/freeze.py
python3 -m unittest discover -s evals/change-pilots/heldout/tests -v

# Substitute an evaluator-controlled launcher for the isolated submitted program.
python3 evals/change-pilots/run.py run --task query-null \
  --corpus-root evals/change-pilots/heldout \
  --command './path/to/isolated-submission-launcher' \
  --report /path/to/evaluator-only-reports/query-heldout.json
```

An explicitly supplied missing/invalid corpus is an error; the runner never falls
back to the public data. Each report includes the selected cases' SHA-256 so runs
can be checked for equivalent grading inputs. Default invocation without
`--corpus-root` still loads only the public corpus.

Freeze the submitted patch when the agent finishes or its predeclared budget expires.
Record the human review decision before revealing held-out results. Grade both
baseline and extension cases and retain separate public/held-out reports. Do not
return held-out failures during development. If private results are later used to
repair code, label that work as follow-up development and use new held-out material
for any further independent measurement.

## Maintenance

The private tests reuse public fixture-consistency checks and also check disjoint
IDs/inputs, case-level coverage notes, and the frozen manifest. Query success
expectations were cross-checked against SQLite with the contract's ordering/type
differences accounted for; SQLite is not the oracle for static errors. Workflow
audits and ledger projections receive additional consistency checks. These checks
do not prove all error decisions or all specified behavior correct, and do not
replace independent review.

`tests/test_discrimination.py` adds two focused fault witnesses: moving a per-row
COALESCE outside SUM, and using a 32-bit accumulator only for refund totals. It
checks that the relevant public cases do not distinguish those faults but new
held-out expectations do. These are narrow semantic fault models, not complete
faulty task implementations or a mutation score for all cases.

After an intentional change to a fixture, public spec, or runner, review it and run
`python3 evals/change-pilots/heldout/freeze.py --write` to record a new manifest.
Then rerun public and private maintenance tests. Do not quietly revise a corpus
between paired language runs; use a new experiment revision or regrade both frozen
submissions consistently, recording why the original experiment needed correction.
