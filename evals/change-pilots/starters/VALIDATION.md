> These are the original 0.18 results. Current 0.22 container checks are recorded in
> [runtime validation](../experiments/VALIDATION-0.22.md).

# Starter validation

Validated on 2026-09-11. All nine variants were built (where needed) and run from
fresh, single-language exports outside the checkout. This verifies starter
readiness; these authoring runs are not measured language-comparison results.

The 237 public and evaluator-only baseline checks passed. All nine positive
extension witnesses returned valid responses that failed the extension contract,
confirming that the requested change remains to be implemented. No fixture cases
were changed or relabeled.

| Task | Language | Public baseline | Held-out baseline | Starter SHA-256 |
| --- | --- | ---: | ---: | --- |
| ledger-refunds | prism | 24/24 | 5/5 | `86e4b4b0746727c1a1668059e3118438e9719a452a5744cd25cc9a0a71968e63` |
| ledger-refunds | python | 24/24 | 5/5 | `5c9d411d039ad4fa48ab40d59de9ccc0d0e928ca240d1abb4a0b8f6e426bda19` |
| ledger-refunds | typescript | 24/24 | 5/5 | `3687fdf65cd6fe8e0f51fa1debe8019660f06be407ed3c27562824905e731de5` |
| query-null | prism | 21/21 | 5/5 | `5a9790f8c9671f73121e6f7c5be81c85b2f6bc764f5db4496d2348e2c89392f7` |
| query-null | python | 21/21 | 5/5 | `12cc12085423f864102bc338d47a6693b5512c877785e8178d5029838d548aa7` |
| query-null | typescript | 21/21 | 5/5 | `8c7b0b7bd6384e5a5fb5703cec1b8363a4a57916417b3f47d2be2aadcc0a93f4` |
| workflow-recovery | prism | 19/19 | 5/5 | `4342b0d35fc9737ca75389470cfd26f77859121d450df9c6766f0fb65eb7d8c8` |
| workflow-recovery | python | 19/19 | 5/5 | `08821804769a009e005872bcde846a26c666d91a460f89680783e815c15e953f` |
| workflow-recovery | typescript | 19/19 | 5/5 | `dc478f115dc990276c5b95a2cffbe0b85ab2730186f75ab47b5ff70a03939b17` |

Other checks: 49 public harness/packaging tests and nine evaluator-only
maintenance tests passed; all three TypeScript projects passed strict typechecking.
Both fixture corpora validate (167 public cases, 77 held-out cases), and the
evaluator manifest matches. The manifest was refreshed only for the workflow
specification sentence acknowledging that starters are now supplied.

Toolchain: Prism 0.18.0, Python 3.14.7, Node 25.2.1, TypeScript 5.9.3,
`@types/node` 25.0.3. Source fingerprints include the explicit export manifest,
source contents, and executable flags.

Reproduce acceptance validation:

```sh
python3 evals/change-pilots/starters/check.py --exported --build --corpus both --verify-incomplete
```

The full matrix was checked, then the TypeScript variants were formatted,
typechecked again, and rechecked using the same command with `--language typescript`.
The fingerprints above reflect those final sources.
