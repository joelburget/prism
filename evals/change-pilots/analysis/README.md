# Evaluation statistics and focused source review

These tools read completed, frozen records and write a separate report directory.
They do not run models, execute submissions, change grades or modify the original
records. Start with the filterable report, select a manageable subset, then open
its stage commits on GitHub or its Prism definition diffs. Identity is visible.

```sh
python3 evals/change-pilots/analysis/report.py --results RESULTS --output REPORT
python3 -m http.server 8766 --bind 127.0.0.1 --directory REPORT
```

The report groups by model, problem, language and checkpoint. It shows exact
behavioral denominators, native-session minutes, tool calls, code/churn size,
resource flags, and separate performance-screen results. Selected run IDs persist
in browser local storage and can be downloaded. No human review measurements are
invented; GitHub review time must be recorded separately.

```sh
python3 evals/change-pilots/analysis/git_reviews.py \
  --results RESULTS --output REPORT --repo CHECKOUT \
  --branch eval-review/COHORT
```

This creates a new branch through a temporary Git index without changing the
current checkout. One baseline commit seeds all chains; each stage has its own
commit against the preceding source for that chain. Exact bytes and executable
modes are retained. Read-only baseline review copies lost their executable bits;
initial modes are recovered from the fingerprint-verified starter and later modes
from the frozen predecessor. For an invalid archive, the commit receipt explicitly says
that `starter/` is the predecessor, not a captured submission. No replacement run
can recover that missing historical snapshot. The output manifest maps run IDs
to immutable commit URLs. Publishing the branch is a separate `git push`.

Prism needs `prism index` (included in 0.22), absent from the original calibration's 0.18.0
compiler. Export from copies using a specified viewer-compatible binary:

```sh
python3 evals/change-pilots/analysis/prism_indexes.py \
  --results RESULTS --output REPORT --compiler /path/to/prism
```

The exporter supplies a temporary project manifest when none exists so the index
includes every module, including `main.pr`. It records that adapter, binary hash,
compiler version, source hashes, missing identities and errors. Each available
comparison has `before.json`, `after.json`, and `diff.json` from `prism index
--diff`. A compilation failure is recorded, not repaired or replaced with a
misleading index. These are review projections, not a regrade with a newer compiler.

The September 2026 report includes a standalone build of the existing Istanbul
workspace's viewer under `viewer/`, with its source revision and file hashes in
`viewer/BUILD.json`. It reads `?src=../prism/RUN/after.json&diff=../prism/RUN/diff.json`.
Re-running `report.py` after generating commits/indexes fills those links.

Run the focused regression tests with:

```sh
python3 -m unittest discover -s evals/change-pilots/analysis/tests -v
```
