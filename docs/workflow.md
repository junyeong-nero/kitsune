# Workflow

How work moves between this repository and
[kitsune-benchmark](https://github.com/junyeong-nero/kitsune-benchmark). This file is
the single source; the benchmark repository links here.

## Route each issue by one question

**Do we need a measurement to know whether it is right?**

| Kind | Example | Where it lives | Who implements |
| --- | --- | --- | --- |
| **Bug**: behavior differs from the docs or expectation, and reproduces | `pointerup` reports `buttons: 1` (#201) | Kitsune issue, `bug` | `implementer` |
| **Decision**: a behavior or design we choose to build | "Separate permission to close from permission to navigate" (#199) | Kitsune issue, `enhancement` or `documentation` | `implementer` |
| **Effect claim**: "this change makes X better" | "A shared `expect` contract reduces invalid expectations" | kitsune-benchmark issue, `hypothesis` | `candidate-builder`, then `hypothesis-verifier` |

If an issue mixes a decision and an effect claim, build the decision here and split the
claim into a benchmark hypothesis. #199 is the example: the protection policy shipped;
"it reduces preservation failures" is a separate hypothesis.

## Hypothesis lifecycle

1. **Open a `hypothesis` issue in kitsune-benchmark** with the template: observation,
   hypothesis, adopt criteria, reject criteria, measurement plan.
2. **Lock the criteria before measuring.** Thresholds are written first. After results
   exist they are not edited; disagreements go in a comment and do not change the verdict.
3. **Build a candidate.** Open a Kitsune issue with the "Hypothesis candidate" template,
   labeled `needs-validation` and linked to the hypothesis. The candidate is switchable
   (option, preset, capability, or one revertible commit) and never changes defaults.
4. **Measure baseline and candidate together.** The baseline is re-run in the same
   measurement as the candidate: same tasks, model, client modes, attempts and
   counterbalanced order. An earlier run is an observation that motivates the hypothesis,
   not the baseline.
5. **Judge against the criteria as written.** Close the benchmark issue with
   `validated`, `rejected` or `inconclusive` and a verdict comment.
6. **Follow the verdict in Kitsune.**

   | Verdict | Kitsune issue |
   | --- | --- |
   | `validated` | Make it the default, remove `needs-validation`, close |
   | `rejected` | Revert or remove the candidate, close with the reason |
   | `inconclusive` | Keep it switchable; record what to re-measure |

Rejected hypotheses stay on record with their reason, so the same idea is not retried
blind.

## Labels

| Repository | Label | Meaning |
| --- | --- | --- |
| kitsune-benchmark | `hypothesis` | Testable claim; criteria set before measurement |
| kitsune-benchmark | `validated` / `rejected` / `inconclusive` | Verdict |
| kitsune | `needs-validation` | Candidate waiting for a benchmark verdict |
| kitsune | `bug` / `enhancement` / `documentation` | Work that needs no measurement |

## Agents

Defined as project agents in `.pi/agents/`.

| Agent | Repository | Input | Stops when |
| --- | --- | --- | --- |
| `implementer` | kitsune | Bug or decision issue | It cannot reproduce a bug locally; acceptance criteria are ambiguous or conflict with [Philosophy](philosophy.md); the issue is mainly an effect claim |
| `candidate-builder` | kitsune | `needs-validation` issue | No linked hypothesis, or its criteria are not locked |
| `hypothesis-verifier` | kitsune-benchmark | `hypothesis` issue | Criteria are missing or not locked; before any model run that costs quota (it returns a run plan first) |

All three work on local branches (`fix/`, `feat/`, `verify/`) in a separate worktree when
the checkout has changes they did not make. **Public actions need a person's approval**:
agents never push, open PRs, comment, label or close. They return drafts.

Agents test against local fixtures and temporary browser profiles. A live site or the
user's own browser is used only when a person asks for it, as in #200's same-screen
comparison.

## What not to claim

- A faster, cheaper or more reliable result without a benchmark verdict.
- That a fix resolves a related report without retesting it. #201 corrected the click
  events; a live retest showed it did not fix #200.
