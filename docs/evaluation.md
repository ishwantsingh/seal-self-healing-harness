# Evaluation design

Self-Heal turns observed limitations into persistent eval cases, then accepts harness changes only when independent checks support them. The first domain is structured table analysis with a fixed contract for filters, grouping, and numeric aggregates.

**Implementation status:** Phases 1–5 are implemented. The oracle remains outside `harness/`; generated candidates run in a local Docker image through a supervisor-owned model/table bridge. Selection plans pin case, dataset, commit, configuration, image, and model identities before comparative trials. Repeated original and private-validation trials, regression checks, evidence completeness, cost limits, and conditional activation are covered by local tests. The original project notes report a live selection rejecting an incorrect candidate after 28 trials; those service records are not included here. No successful live model-generated promotion or untouched final result is established by this copy.

## Creating an eval from an observation

The supervisor records the original question, interpreted task when available, assigned Atlas dataset ID/content hash, environment references, LangSmith trace ID, source/model/configuration identities, observed result, and violated requirement in Atlas. It reads model/tool calls and errors from LangSmith when diagnosing the incident. An explicit `outcome=unsupported` is tagged as a capability gap and is a trigger alongside a wrong result, resource exhaustion, or exception. It is not a successful answer merely because the conversational CLI exited normally. A zero-match total, malformed model response, or provider failure keeps its own classification.

The model proposes a structured scenario within the supported contract. `evals/analyst/generator.py` validates that proposal and materializes its rows as a new immutable Atlas dataset through the trusted adapter. `evals/analyst/oracle.py` computes the correct result independently of the candidate. Freeze the dataset ID/hash, case, and expected result before patch generation, then replay the baseline on both the observed task and the generated case. The new case must also fail the old harness; separate fresh Atlas datasets stay hidden for candidate validation.

An exception or unsupported response does not supply the correct answer. A capability gap can drive a behavioral change only after the requested behavior, necessary data access, and independent expected result have been defined and validated. A request outside the current contract needs a protected contract/oracle extension frozen before candidate comparison. If it requires data or tools outside the trusted interface, retain the gap and proposed interface extension as an open item. Without trustworthy ground truth, the supervisor may investigate or improve diagnostics, but must not invent an expected answer just to complete the cycle.

A scenario record should identify:

- Task family, user-visible task, structured query/requirements, and incident provenance.
- Original unsupported question, refusal reason, requested capability, and capability-gap classification when applicable.
- Atlas dataset ID, row count, generation parameters, seed reference, and content hash.
- Contract/oracle version and the protected expected-result reference.
- Correctness invariants and externally enforced resource limits.
- Case identity, declared evaluation role, creation time, and exposure history.

The model may propose tasks and variations; it cannot edit the generator, reference calculation, grading rules, or budgets. Private seeds, dataset IDs, and answers remain outside its accessible workspace and table interface.

## Evaluation roles

| Role | Purpose | Exposure and lifecycle |
| --- | --- | --- |
| Original reproduction | Verify the observed limitation and its resolution | Disclosed to the proposer after validation; retained permanently as a regression |
| Existing regressions | Preserve known capabilities and previously fixed cases | Development evidence; never silently delete a failure to improve the score |
| Fresh validation | Check transfer while deciding whether to accept a candidate | Generated independently of the proposed patch, private initially; feedback used in selection is development evidence |
| Final assessment | Assess the selected, frozen harness on untouched tasks | Excluded from candidate selection and tuning; report all outcomes after selection |

Fresh validation is not a pristine final test merely because the proposer has not seen its inputs. Repeated pass/fail feedback influences selection. Limit patch attempts, retain all results, and record when a case becomes exposed.

If final-assessment feedback guides further development, that case becomes development evidence. A later final assessment requires new untouched cases and disclosure of the prior attempts. A failed final assessment cannot be erased or renamed into a successful demonstration.

## Comparing baseline and candidate

Run the old and new harness against the same immutable Atlas dataset ID/hash, with fresh read cursors and counters, and record the same model/settings and execution environment. Detect a changed row count or content hash before comparing trials. The baseline must fail the original requirement; the candidate must satisfy it. Neither is allowed to alter task truth or increase the acceptance budget.

For a capability-gap case, reproduce the baseline's `unsupported` outcome on the original natural-language question. The candidate must produce the independently graded answer on that question and fresh paraphrases/data variations; merely suppressing the refusal is not improvement. Keep honest refusals for requests still outside scope in the regression bank.

Grade outside candidate execution. LangSmith traces explain behavior and supply reported model usage; the protected grader and runner remain authoritative for correctness, tool-call count, Atlas page/byte counts, elapsed time, and budget decisions. Provide the candidate only its task and a trusted table interface bound to the assigned Atlas dataset; do not mount table snapshots, the oracle, expected answers, private controls, credentials, or entire repository. The trusted interface rejects cross-dataset access and arbitrary MongoDB queries. A candidate cannot self-report the authoritative pass/fail result or resource count.

Measure:

- Exact task correctness under the declared output contract, including omissions, duplicates, and invalid outputs where relevant.
- Model/tool calls, Atlas pages/bytes read, externally observed token usage, elapsed time, and budget violations.
- Individual trial results for the original case, existing regressions, and fresh validation.
- Source/configuration/environment identities, Atlas dataset ID/hash, LangSmith trace ID, and the mechanism changed by the patch.

A lower token count does not compensate for a wrong answer. Additional work is permitted only within the fixed, declared acceptance limits. The comparison must not silently change models or give the candidate extra retries.

Deterministic Atlas datasets and a deterministic oracle stabilize grading; they do not make an LLM's choices deterministic. Use scripted model responses for component checks and a few repeated live trials for critical end-to-end behavior. Preserve every trial and report observed variation rather than claiming statistical certainty from a small sample.

## Analyst variations

For a candidate that adds aggregation and changes context selection, use several supported variations:

- Reorder rows and rename entities so answers cannot depend on input order or known names.
- Change table size, group counts, and the supported grouping or filter field.
- Include empty results, zero totals, offsetting entries, and missing values with contract-defined semantics.
- Keep small-table questions that already worked before the change.

Reference answers come from the protected calculation, not the generated aggregation tool. Validate the oracle itself against hand-checked examples before relying on it. The generator varies data and requirements; it does not produce a replacement authoritative grader for each proposed patch.

A fresh question over a new Atlas dataset tests reuse within this family. It does not establish cross-domain generalization.

## Acceptance and reporting

The selection gate requires a reproduced original failure, a screened in-scope diff, a passing original case and regression suite, passing fresh validation under fixed budgets, sufficient execution evidence, and matching candidate/evaluation identities. Where repeated trials are required, their acceptance rule is fixed before candidate testing.

Keep proposed changes attributable: one coherent mechanism may include a tool and the context guidance needed to use it. Check for embedded answers, fixture-specific values, unauthorized files, and weakened limits before full evaluation. A leakage screen is useful evidence, not a proof that overfitting is impossible.

Record rejections with their hypotheses, diffs, scores, costs, and reasons. Later proposals should consult that history. Reevaluate any proposed removal of an existing mechanism rather than silently pruning it.

After selection, pin the chosen version and run the final assessment once according to the reserved protocol. Store and present its results separately from the acceptance score. A diagnostic improvement, accepted candidate, successful fresh execution, and successful final assessment are distinct outcomes.

## Research alignment

[RRSI](https://arxiv.org/html/2609.24972v2) motivates controlling how finite feedback drives persistent harness edits. Our small implementation borrows evidence history, attributable changes, leakage checks, and attention to noise/cost. Automatically producing validated eval cases from operational observations is our proposed workflow, rather than a claim that the paper implements it.
