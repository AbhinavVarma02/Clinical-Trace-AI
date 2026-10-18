# UI polish: less reading, clearer results

Frontend-only simplification of the React workspace (`frontend/workspace/`). No backend, model, Worker or deployment change. **Published 2026-10-19 (owner-approved)** as edge release `475f0b6`; the backend and Space were not touched.

## Principle

Less reading. More interaction. Clearer results. Essential trust and safety statements stay visible; engineering depth moves behind labelled disclosures.

## What changed

| Area | Before | After |
| --- | --- | --- |
| Overview first screen | Title, 3-line subtitle, guided-case card with long paragraph, warning banner, workspace note, four stat cards | Hero: "Understand patient records. See the evidence.", one supporting sentence, a single primary **Try a guided case** button, one-line safety note, four short steps |
| Overview body | Status cards, scenario cards with patient id and question, a six-card evidence grid, model card, patients list | One status line, four short scenario cards, four headline results, a visible calibration limit, **All measurements and limits** disclosure |
| Guided steps | Two-line descriptions | One short line each; progress is still recorded only by real actions |
| Agent | Long page intro; suggestions in a side card | One-line intro; suggested questions inside the empty state; **Sources** label before citations; first 5 claims shown with **Show all N verified claims** |
| Risk | Eight stacked cards | Probability, threshold, calibration (kept visible), SHAP, caveats (kept visible); benchmark metrics and mapped inputs behind **Model details** |
| Evidence / Evaluation | Dense diagnostics subtitles and run history always open | Short subtitles; diagnostics and run history behind disclosures |
| Banners and hints | Multi-sentence availability, quota and guide messages | One or two sentences each, same meaning |

## Kept on purpose

Synthetic-data and not-clinically-validated notice (hero and sidebar), calibration limit on the first screen, deterministic versus live-LLM labelling, claim verification counts, removed-claim lists, citations with source version, human review bound to the exact artifact hash, persona isolation and audit trail. Real agent execution is unchanged; nothing is mocked or precomputed.

## Accessibility and theming

- Native `<details>` disclosures (keyboard and screen-reader operable, visible focus).
- Dark theme: primary buttons now use a dark label (`--on-accent`); white on the dark accent measured 2.8:1.
- Light theme: `--text-faint` darkened to meet 4.5:1 for small text.
- Patients page gets a real page-level `<h1>`. Every page has exactly one `<h1>`, no unnamed controls and no horizontal overflow at 390 px, in both themes (scripted audit, 8 pages x 2 viewports x 2 themes).

## Measured text reduction (visible words, 1440 px, same API)

| Page | Before | After |
| --- | --- | --- |
| Overview | 614 | 236 (-62%) |
| Risk | 401 | 261 (-35%) |
| Evidence | 58 | 39 (-33%) |
| Agent (empty) | 170 | 137 (-19%) |
| Evaluation / Patients / Operations / Governance | 1,853 | 1,746 (-6%; mostly data tables, not explanatory copy) |

## Verification

- `npm run check`: TypeScript, 41 Vitest tests, both builds.
- Playwright: 30 passed (desktop, tablet, mobile): four guided scenarios with real streaming, citations, review approval, persona isolation, cold start.
- Screenshots (before and after, desktop, tablet, mobile, dark): [docs/images/ui-polish/](images/ui-polish/).

## Releasing

Released with `cd deploy/cloudflare && npm run release -- --full` (see [DEPLOYMENT.md](DEPLOYMENT.md)); the full public smoke passed on the first attempt, so no rollback was needed. README line 9 now says **Try a guided case**. Rollback if ever needed: `npx wrangler deployments list`, then `npx wrangler rollback <previous-version-id>` (previous release `7012e8e`).

## Verification badge follow-up (2026-10-19)

The sample below is historical. The subsequent unreleased backend reliability
continuation, including every retained development experiment and its limitations,
is documented in [AGENT_RELIABILITY.md](AGENT_RELIABILITY.md). The badge continues
to display actual accepted/excluded counts and deterministic fallback status.

The public guided case once showed "7/12 claims verified". Investigation:

**Semantics.** `claims_total` is every claim the composer drafted; `claims_supported` is those that passed the verifier (valid same-patient citation, numbers present in or recomputed from the cited evidence, lexical support at least `MIN_LEXICAL_SUPPORT`, and the output-safety patterns). Failing claims are removed from the answer and kept in `answer.rejected_claims` with reasons. `7/12` therefore meant "7 shown, 5 removed", which read like a failed check.

**Sample (live Qwen3-30B via the deployed Workers AI proxy, repo backend, 48 runs over six questions, plus 11 runs through the public edge).**

| Finding | Result |
| --- | --- |
| LLM-composed runs | 44 of 48; 4 fell back to the deterministic composer |
| Claims drafted / kept / excluded | 250 / 203 / 47 (19% excluded) |
| Runs with at least one exclusion | 12 of 44 |
| Clean on every run (8 of 8) | Contradictory notes, missing information, metformin; HbA1c was clean in 6 of 8 |
| Weak spots | The guided case "Summarize this patient journey" (all 8 runs imperfect: 4 fell back after the model returned unparseable JSON, 4 excluded 36-73% of claims) and the warfarin question (6 of 8 runs excluded 2-4 claims) |
| Why claims were excluded | 30 low lexical support, 19 output-safety pattern, 1 number not in the cited evidence (reasons overlap) |

**What the reasons mean.** Most exclusions are benign mismatches rather than wrong facts: "January 2025" versus an ISO date, "percent" versus "%", "was diagnosed with" or "was prescribed" tripping the clinical-instruction patterns although the sentence restates the record. A minority are genuinely unsupported inferences ("before the warfarin dose was reduced", "due to declining kidney function") or a number not in the source. The verifier is conservative by design, so the failure mode is dropping true claims, not keeping false ones. Loosening the safety patterns for past-tense record statements would change safety policy and was not done; it is listed as a candidate improvement for the owner to decide.

**UI change (wording only; verifier, counts and rejected claims unchanged).**
- Badge: "7 evidence-backed claims" plus a clickable "5 excluded" that opens the exclusion list. The tooltip keeps the exact ratio ("7 of 12 drafted claims were supported ... and kept").
- The list is titled "Excluded draft claims (5) · why they were removed". Each claim shows a plain-English label (for example "Wording not supported by the cited record") followed by the verifier's raw reason.
- When the model's draft was unusable and the rule-based composer wrote the answer, the badge now says "Deterministic fallback · LLM draft unusable" (or "failed verification") instead of "Live LLM". Previously these answers were labelled "Live LLM" although the text came from the deterministic composer.
