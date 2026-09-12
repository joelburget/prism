# Evaluator-only materials

Never export or mount this directory, its sibling maintenance tests, or their Git
history in an implementation agent's environment. Use the allowlisted exporter.

* `heldout/`: frozen expected-output cases and per-case coverage notes.
* `workflow_model.py`: leased-mode transition model for fixture authoring.
* `query_model.py`: selected-query recomputation oracle using SQLite.
* `generate.py`: fixed scenarios and seeded long traces; copies all original
  public/private cases into the corresponding regression partition.
* `oracle_cli.py`: wire smoke-test adapter for new fixtures only. It is not a
  complete or eligible candidate implementation and does not support legacy mode.
* `scaling.py`: separately measured workload profiles, with correctness checks and
  explicitly uncalibrated performance thresholds.
* `grade.py`: fresh-container behavioral grading for frozen submitted sources.
* `freeze.py` and `MANIFEST.json`: independent versioning for checkpoint two.

The two discrimination tests demonstrate faults surviving the **new public
extension cases**, not the full previous suite: the optimizer mistake would also
be caught by some retained checkpoint-one regressions if applied to its legacy
path. A plausible extension-specific error is applying that unsafe transformation
only in incremental maintenance. Fault witnesses are bounded claims, not a mutation
score over all realistic implementations.
