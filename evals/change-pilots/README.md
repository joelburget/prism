# Prism change-evaluation pilots

Three modification tasks for comparing agents working in Prism and another language:

| Pilot | Existing program | Requested change | Baseline / extension cases | Contract and acceptance cases |
| --- | --- | --- | --- | --- |
| `query-null` | Relational query engine | SQL NULL, three-valued logic, and left outer joins | 21 / 34 | [Problem](query-null/PROBLEM.md), [cases](query-null/cases.json) |
| `workflow-recovery` | In-memory workflow runner | Durable recovery, retries, cancellation, and idempotent external actions | 19 / 38 | [Problem](workflow-recovery/PROBLEM.md), [cases](workflow-recovery/cases.json) |
| `ledger-refunds` | Append-only ledger and invoice system | Partial refunds and compensating reversals | 24 / 31 | [Problem](ledger-refunds/PROBLEM.md), [cases](ledger-refunds/cases.json) |

This directory contains problem descriptions, public acceptance fixtures, an additional [evaluator-only held-out corpus](heldout/README.md), a shared executable runner, and [baseline starter implementations](starters/README.md) in **Prism, Python, and TypeScript for all three tasks**. Completed extension solutions are not included. Each language is evaluated in a separate agent run using the same process protocol and public cases. A run receives only its assigned language's starter. The Python test driver is independent of the implementation language.

Each problem specifies the baseline implemented by the starters, the requested extension, observable behavior, and boundaries on scope. Compare extension runs from frozen starter revisions. An agent modifying a starter must preserve its existing public interfaces as well as satisfy this external acceptance contract; the black-box suite cannot enforce internal source compatibility on its own.

## Separate language runs

Run Prism and the comparison language in fresh, separate agent sessions. Each session receives the common task specification and public tests, its assigned starter, and the appropriate language documentation/toolchain. It must not receive the other language's starter, solution, patches, transcripts, review findings, or evaluation results. Prepare isolated run inputs that exclude those artifacts from both working files and accessible Git history; putting two implementations in different folders or worktrees of an accessible repository is insufficient isolation. Do not carry conversation context or task-specific solution notes from one run into another.

Freeze the common specification, fixtures, and experimental settings before either run. Apply the same predeclared budget policy and score the submitted artifacts afterward. A shared suite means evaluator reuse across independent runs, not simultaneous access to both implementations. The process runner below does not provision or enforce this agent isolation.

The committed `heldout/` directory and its Git history belong to the evaluator.
Create each agent's public input with an allowlisted export selecting one task
and one language, then supply its language tooling:

```sh
python3 evals/change-pilots/export_public.py --task query-null --language prism --output /path/outside/this/repository/query-prism
```

The output directory must be new and outside this evaluator repository. It contains
only the selected task's public description/cases, runner, usage instructions,
one language's `starter/` sources, and a hash manifest. Omit `--language` for a
tests-only bundle. It excludes other implementations, the private corpus,
coverage notes, reports, other tasks, and Git metadata. Run the export in an environment where the implementation
agent cannot read this source checkout; copying alone does not restrict access.

## Run the suite

Requires Python 3.10+ and macOS or Linux, with no third-party Python dependencies. Run these from the repository root:

```sh
# Validate the corpus structure and print case counts. Does not execute a solution.
python3 evals/change-pilots/run.py validate

# Inspect baseline or extension cases without running them.
python3 evals/change-pilots/run.py list --task query-null --phase extension

# Check the harness and consistency of expected results, not solution conformance.
python3 -m unittest discover -s evals/change-pilots/tests -v
```

Build Prism once before testing. The Python and TypeScript launchers run source
directly (see starter READMEs for runtime requirements):

```sh
./evals/change-pilots/starters/query-null/prism/build.sh
python3 evals/change-pilots/run.py run --task query-null --phase baseline \
  --command './evals/change-pilots/starters/query-null/prism/run.sh' --report evals/change-pilots/reports/prism-baseline.json

python3 evals/change-pilots/run.py run --task query-null --phase baseline \
  --command './evals/change-pilots/starters/query-null/python/run.sh'

python3 evals/change-pilots/run.py run --task ledger-refunds --phase baseline \
  --command './evals/change-pilots/starters/ledger-refunds/typescript/run.sh'

# Verify all nine starters, including that positive extension work is still missing.
python3 evals/change-pilots/starters/check.py --build --verify-incomplete
```

The command is split into arguments using `shlex`, then executed **without a shell**. Paths containing spaces need quotes inside the command string. Compile once before the run; use a small launcher script if needed. `--cwd` sets the implementation's working directory; otherwise it inherits the current directory. Task selection is repeatable; omitting it selects all three tasks and requires one executable that dispatches on the request's `task`. A single-task export must use `--task` and does not need other tasks' files. `--case CASE_ID` selects exact IDs within the task/phase selection and is repeatable. An invalid or empty selection is an error, never a pass.

All subcommands accept `--corpus-root DIRECTORY`, where `DIRECTORY/TASK/cases.json`
contains the requested corpus. The default is the public data next to `run.py`;
private cases are never loaded implicitly. Evaluator usage:

```sh
python3 evals/change-pilots/run.py validate --corpus-root evals/change-pilots/heldout
python3 evals/change-pilots/run.py run --task query-null \
  --corpus-root evals/change-pilots/heldout --command './path/to/isolated-submission-launcher' \
  --report /path/to/evaluator-only-report.json
python3 -m unittest discover -s evals/change-pilots/heldout/tests -v
```

Keep the corpus, terminal output, and reports evaluator-only. The submission launcher
must not expose the evaluator filesystem to submitted code. Reports record a SHA-256
of selected cases, including expected results; `heldout/MANIFEST.json` additionally
freezes both complete corpora, public specs, and the runner. See [held-out maintenance
instructions](heldout/README.md) for manifest verification and revision.

Every fixture launches a fresh process. Default timeout is 10 seconds per fixture, configurable to account for runtime startup. Timeout is a hang guard, not a performance score. Stdout and stderr each have a 1 MiB limit. Timeouts and output overflow terminate the process group. Use trusted launchers: this runner is **not a security sandbox**, does not deny network/filesystem access, and cannot stop a deliberately detached process. Evaluation isolation and agent budgets belong in the surrounding experiment environment.

Exit codes: `0` means every selected case passed; `1` means at least one acceptance failure; `2` means invalid corpus/arguments, launch failure, or inability to write the report. A launch failure stops execution and reports remaining selected cases as `not_run`.

## Language-neutral wire protocol

The runner writes one UTF-8 JSON value followed by a newline to stdin, then closes stdin:

```json
{"protocol_version":1,"task":"query-null","input":{"...":"task-specific input"}}
```

The implementation must consume the request and write exactly one JSON response to stdout, then exit with status zero. A response can be pretty-printed, and surrounding whitespace is permitted. Diagnostics may go to stderr. Additional stdout text, multiple JSON values, invalid UTF-8, duplicate object keys, and nonzero exit codes fail the case.

There are exactly two outer response forms:

```json
{"ok":true,"result":{"...":"task-specific output"}}
```

```json
{"ok":false,"error":{"code":"TASK_SPECIFIC_CODE"}}
```

All fixtures use protocol version 1 and a valid outer request envelope. The task documents define validation of `input`, domain-error codes, and whether errors within an operation sequence are embedded in a successful outer result. Adapter behavior for other protocol versions or malformed outer envelopes is outside this pilot.

Comparison is exact structural JSON equality:

- Object key order and JSON whitespace do not matter; array order does.
- No extra or missing response keys are allowed.
- Booleans, integers, strings, and null are distinct (`true` does not equal `1`).
- Numbers use integer JSON syntax; floating point tokens, exponents, NaN, and Infinity are rejected. Each task specifies its integer bounds and arithmetic semantics.
- Strings are compared as decoded Unicode strings, without normalization.
- Each task specifies deterministic row, event, and report ordering. The runner does not silently sort outputs or erase fields.

The process wrapper must be thin: decode JSON, invoke the program being evaluated, encode JSON. An implementation must not call another language's solution, the fixtures, or an expected-output lookup table to produce answers. The harness does not itself enforce this source-level rule.

## Fixture format and acceptance

Each task's `cases.json` has `schema_version: 1`, its task identifier, and a `cases` array. Each case has exactly:

| Field | Meaning |
| --- | --- |
| `id` | Stable ID, unique within that task |
| `phase` | `baseline` for existing behavior, `extension` for the requested changes |
| `description` | Behavior being checked |
| `input` | Task-specific object, wrapped by the harness in the wire request |
| `expect` | Entire expected wire response |

Multiple operations in one fixture share logical state; different fixtures do not. Observations in the middle of a sequence test historical and intermediate behavior, rejected-operation atomicity, and interactions after failure—not only the final state. Every starter must pass all baseline cases. A completed modification must pass **both** phases using the same executable. The existing phase partition is the starter acceptance set; cases have not been moved or weakened to accommodate implementations.

Results are reported separately for each task and phase. Full acceptance requires every selected case to pass; case pass rates are useful diagnostics, not an unbiased cross-task difficulty score. Feature groups should receive deliberate weights if partial credit is used later. A compound case can check several requirements, so a failure count is not a defect count. Store the repository revision, starter revision, toolchain versions, agent/model settings, prompts, and resource budgets alongside each experiment's report; the runner records the command, cwd, timeout, and per-case results.

## Oracle and coverage limits

A full reference implementation is **not required** to run these tests: each fixture contains concrete expected behavior. The problem descriptions are the intended specification; if a fixture contradicts one, resolve and version the discrepancy before scoring a run. Expected outputs still need scrutiny. They should be checked again when implementing the starters, and any disagreement between languages is evidence to investigate, not proof that one is correct.

The maintenance tests in `tests/test_corpus.py` check paired optimizer expectations, workflow service-audit/effect consistency and snapshot invariants, and ledger receipt/audit consistency and signed accounting projections. They trust the fixtures' operation-acceptance decisions and do not implement the SQL evaluator, workflow scheduler, or ledger validation rules. They are checks against mistakes in the expected data, not a substitute for independent review or a complete reference solution.

The root task directories contain public, fixed pilot cases; `heldout/` adds new evaluator-only cases without moving or removing public cases. Neither corpus is an exhaustive proof. The suite tests externally observable behavior; it cannot establish that a program uses a particular architecture, preserves unexposed APIs, performs real crash-safe filesystem writes, or scales to production load. Workflow durability is modeled through the specified crash/restart simulator. Broader process-kill and disk fault testing can be added as a separate integration layer later.

The held-out set targets additional combinations and boundary behaviors from the existing public contracts. Its private coverage notes explain each case's difference from the public corpus and the faulty generalization it is designed to expose. These are reasoned test-design hypotheses, not measured mutation-testing results. Iterating on public tests is allowed and is part of normal development; held-out results measure whether the submitted change also handles unseen instances of the stated requirements. Review and timing remain separate measurements, and a quickly produced or easily reviewed patch can still be incorrect.

Keep both corpora frozen before the implementation runs and use the same held-out cases for the separate language runs. Keep private data, authoring conversations, and any private generators outside all implementation-agent-accessible files and Git history, not merely in a gitignored directory. Do not expose held-out feedback during development; evaluate frozen submissions after the agent stops. If results are subsequently used for repairs, label those as follow-up development and use fresh held-out material for a new independent evaluation.

Useful further checks include generated optimizer-on/off query equivalence, recovery versus uninterrupted workflow outcomes, and ledger event-prefix/reconciliation invariants. The shipped fixtures are fixed scenarios, not a generative test system. Avoid defining new semantics only through hidden tests.

Human review is a separate measurement: freeze the submitted patch and record review time, defects found, false alarms, comprehension answers, and confidence before revealing held-out results. Neither passing this suite nor a small diff demonstrates reviewability.
