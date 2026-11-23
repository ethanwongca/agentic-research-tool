# Research pipeline protocol (arx)

This repo is an **arx-governed research pipeline**. `pipeline.yaml` is the researcher's spec: the stages
(boxes), their inputs/outputs (arrows), the method (`approach`), the decisions, the constraints, and the
checks that define "correct". **The spec overrides your judgment.** Your job is to write the code the
researcher has already designed, not to design it. **The researcher drives the ideas; you implement.**

## Hard rules
0. **Don't drive ideas.** Never propose stages, methods, models, parameters, metrics or new checks. The
   researcher adds every box on the board. You may point out *that* something is unspecified, as a
   neutral question, but not *what* it should be.
1. **Never make a research decision the spec leaves open.** Models, parameters, thresholds, libraries,
   preprocessing steps, seeds, how edge cases are handled: if it isn't specified, STOP and ask the researcher.
   Record the question with `arx ask <stage> "<q>" --key <param>` (no `--option` unless the policy
   allows it) and their answer with `arx answer <stage> <key> "<answer>"`. Never answer on their behalf.
2. **Only implement stages that `arx status` shows as APPROVED or FAILING**, and only after every upstream
   stage is VERIFIED. Start with `arx scaffold <stage>`, which generates the module from the spec.
3. **Read every choice from `params`**, never hardcode it. `run(inputs, params, ctx)` returns a dict with
   exactly the declared outputs.
4. **A stage is done only when `arx verify <stage>` passes.** It is then LOCKED: never edit a VERIFIED
   stage's files. If you think verified code is wrong, explain why and ask the researcher to `arx unlock`.
5. **Never weaken checks.** Don't edit `pipeline.yaml` or `.arx/`, and never run `arx approve`/`arx unlock`.
   Those belong to the researcher.
6. Keep stage logic in the stage's declared `files:`, so verification and locking cover it.
7. **Failures go back to the researcher.** Fix implementation bugs, but never change the method to get
   past a check. After `max_attempts` failed verifies the stage is BLOCKED. Report facts (failing checks,
   errors, observed data, what you tried) and stop. The researcher decides the next box.

## Commands
| command | who | what |
|---|---|---|
| `arx status` / `arx next` | anyone | what's blocked, and what to do next |
| `arx check [stage]` | anyone | everything still unspecified |
| `arx ask` / `arx answer` | agent | raise a question / record the researcher's answer |
| `arx scaffold <stage>` | agent | generate the stage boilerplate from the approved spec |
| `arx run [stage]` | anyone | run on real data, print output summaries |
| `arx verify <stage>` | anyone | run tests + invariants; lock on pass |
| `arx ui` | researcher | boxes-and-arrows editor: fill in specs, answer, approve, unlock |
| `arx approve` / `arx unlock` / `arx retry` | researcher only | gate transitions (interactive terminal) |

## Review a spec ("is this enough?")
For each non-verified stage, ask: could I implement this without making any research decision myself?
Look for unpinned method details, uncovered edge cases, underspecified output types or shapes, and holes
in the checks. Raise each one as a **neutral** question with `arx ask` (no suggested answers, no new
stages), ask the researcher, record their answers, then list what's still open per stage.

## Build the next stage
`arx next` → if APPROVED: `arx scaffold` → implement → `arx verify` → fix the implementation until
it passes → stop and report. If DRAFT/READY/BLOCKED: stop and hand back to the researcher.
