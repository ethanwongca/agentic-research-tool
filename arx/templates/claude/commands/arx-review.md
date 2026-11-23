---
description: Point out what the researcher's spec leaves unspecified, as neutral questions. No ideas, no answers.
argument-hint: "[stage]"
---
The researcher owns the ideas. Your only job here is to find where the spec is **not specific enough to
implement without you making a research decision**, and ask about it. Target: $ARGUMENTS (all
non-verified stages if empty).

1. Run `arx check --json` and read `pipeline.yaml`.
2. For each target stage, list the places where you would otherwise have to choose something yourself:
   - details the `approach` leaves open (hyperparameters, thresholds, library/version, seed)
   - edge cases nothing addresses (empty input, duplicates, NaN, encoding)
   - outputs whose type, shape, ordering or dtype isn't pinned down
   - checks a plausibly wrong implementation would still pass. Point out the hole ("nothing checks X"),
     but don't write the check.
3. Record each one as a **neutral question**: `arx ask <stage> "<question>" --key <param_name> --why "<what depends on it>"`
   - Don't pass `--option` unless the policy has `agent_options: true`. Don't phrase questions so they
     lead to an answer ("Should we use HDBSCAN?" leads; "Which clustering algorithm?" doesn't).
   - Never suggest new stages, splitting stages, or different methods.
4. Ask the researcher the questions with AskUserQuestion (up to 4 at a time). Record their answers
   verbatim: `arx answer <stage> <key-or-index> "<answer>"`.
5. Finish with a per-stage list of what's still open, and remind the researcher to approve in the flow (`arx ui`).

Never answer a question yourself, never edit pipeline.yaml, and never write stage code here.
