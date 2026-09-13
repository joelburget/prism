# Chained calibration results

96 stages · 67 behavioral passes

## By model and checkpoint

| model | checkpoint | Pass | Median min | Unflagged min | Flagged | Perf |
| --- | --- | --- | --- | --- | --- | --- | --- |
| astra | 1 | 6/6 | 2.4 | 2.4 | 0 | 0/0 |
| astra | 2 | 5/6 | 4.5 | 3.6 | 1 | 2/2 |
| fable | 1 | 6/6 | 4.4 | 4.4 | 0 | 0/0 |
| fable | 2 | 6/6 | 9.1 | 8.9 | 1 | 1/3 |
| haiku | 1 | 0/6 | 5.6 | 7.6 | 2 | 0/0 |
| haiku | 2 | 0/6 | 7.0 | 6.2 | 2 | 0/0 |
| luna | 1 | 6/6 | 3.1 | 3.1 | 0 | 0/0 |
| luna | 2 | 0/6 | 3.2 | 3.2 | 2 | 0/0 |
| opus | 1 | 6/6 | 5.7 | 5.7 | 0 | 0/0 |
| opus | 2 | 6/6 | 15.3 | 14.4 | 1 | 2/3 |
| sol | 1 | 5/6 | 3.6 | 3.6 | 0 | 0/0 |
| sol | 2 | 6/6 | 5.6 | 5.6 | 1 | 0/3 |
| sonnet | 1 | 4/6 | 5.5 | 5.1 | 2 | 0/0 |
| sonnet | 2 | 4/6 | 17.5 | 5.6 | 3 | 0/1 |
| terra | 1 | 4/6 | 2.3 | 2.2 | 1 | 0/0 |
| terra | 2 | 3/6 | 2.7 | 2.8 | 1 | 0/1 |

## By problem, language and checkpoint

| task | language | checkpoint | Pass | Median min | Unflagged min | Flagged | Perf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| query-null | prism | 1 | 4/8 | 9.0 | 9.0 | 2 | 0/0 |
| query-null | prism | 2 | 3/8 | 8.0 | 7.2 | 5 | 0/3 |
| query-null | python | 1 | 7/8 | 5.1 | 5.1 | 0 | 0/0 |
| query-null | python | 2 | 5/8 | 5.5 | 5.4 | 1 | 2/5 |
| query-null | typescript | 1 | 5/8 | 3.8 | 3.8 | 2 | 0/0 |
| query-null | typescript | 2 | 5/8 | 7.5 | 8.7 | 2 | 3/5 |
| workflow-recovery | prism | 1 | 7/8 | 3.0 | 3.0 | 0 | 0/0 |
| workflow-recovery | prism | 2 | 5/8 | 10.0 | 9.8 | 3 | 0/0 |
| workflow-recovery | python | 1 | 7/8 | 2.4 | 2.4 | 0 | 0/0 |
| workflow-recovery | python | 2 | 6/8 | 3.0 | 3.0 | 0 | 0/0 |
| workflow-recovery | typescript | 1 | 7/8 | 2.4 | 2.2 | 1 | 0/0 |
| workflow-recovery | typescript | 2 | 6/8 | 3.8 | 4.4 | 1 | 0/0 |

## Interpretation

- One rollout per model/task/language cell. Checkpoint two inherits the same model’s frozen checkpoint-one source in a fresh session.
- Behavioral pass requires every public and held-out case. Invalid archives count as failures; their source and code metrics are unavailable.
- Time is native-session elapsed time, including tools, excluding setup and external grading. It is not human coding time.
- Resource flags are retained. Unflagged medians exclude all flagged stages; pass rates always include them.
- Code lines and churn include all .pr/.py/.ts files, including model-written tests, blank lines and comments. They are review-size proxies, not quality scores.
- Performance screens cover two restricted query profiles and only behaviorally passing follow-ups. A screen pass is not a general incremental-algorithm proof.
- Token usage is provider-native and not normalized across providers. API-equivalent cost is an estimate, not subscription billing.
- Model identity is visible in this report and its GitHub links. No human review times or comprehension scores have been recorded by this exporter.
