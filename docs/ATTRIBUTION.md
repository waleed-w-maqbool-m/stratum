# Where Stratum's failures come from

Stratum answered 28 of the 46 gold-scored Chinook questions correctly (60.9%, model
`gpt-oss-120b`). This traces each of the 18 failures to the stage that caused it. The
per-failure record, with the evidence and every query, is
[gauntlet/reports/chinook_v1.attribution.json](../gauntlet/reports/chinook_v1.attribution.json).

## Result

| Cause | Failures | What it means |
|---|---:|---|
| Retrieval | 8 | The semantic model had what was needed; the wrong entities or values were surfaced |
| Benchmark | 4 | The question, gold answer or scorer is at fault, not Stratum |
| Excavation | 3 | The semantic model is wrong or incomplete |
| Generation | 2 | The right context was supplied and the language model still wrote a wrong query |
| Compilation | 1 | The logical SQL was right and the compiler turned it into a wrong physical query |

Two things stand out. Only 2 of 18 failures are the language model writing a wrong
query. And 5 of the 8 retrieval failures share one root: the excavation step named the
track table `product`, so "tracks" linked to the `playlist_track` bridge table, the join
plan was rooted there, and counts were multiplied by playlist membership.

## Method

The categories were fixed before looking at the cases. Each failure was then tested two
ways, so the label does not rest on reading the SQL.

1. **Counterfactual.** Write a correct logical query against Stratum's own semantic model,
   compile it with Stratum's compiler, run it, and score it exactly as the benchmark does.
   If it passes, the model and compiler were sufficient and the fault is upstream
   (retrieval or generation). If it fails, the fault is in the model or compiler, or in the
   benchmark. 14 of 18 pass; the 4 that fail are the three excavation cases and the
   composer case.
2. **Intervention.** For the cases that passed, re-ask the question but supply the correct
   entities, and see whether Stratum now answers correctly. If it does, retrieval was the
   fault; if not, generation was. Each was run up to three times.

The semantic model used is version 1, which reproduces all 48 recorded physical queries
exactly.

## The 18 failures

| Cause | Questions | Evidence |
|---|---|---|
| Excavation | c-005, c-013, c-036 | The model exposes 33 country spellings where the clean data has 24 (`USA` and `U.S.A.` stay separate). A correct query over the model returns 420.07 against gold's 523.06 |
| Compilation | c-021 | A valid anti-join, but the compiler pushed `album.id IS NULL` into the left join, so every artist looked album-less (275 rows against 71) |
| Benchmark | c-006, c-042 | Exactly the same ten customers as gold, differing only in the order of tied totals under an order-sensitive scorer |
| Benchmark | c-025 | At least five cities tie at two customers; gold picks Berlin alphabetically |
| Benchmark | c-012 | The legacifier replaced the real composer of 74 tracks with a blank or marker, so gold cannot be recovered from the legacy data (below) |
| Retrieval | c-010, c-019 | The right entity was not linked or the value was not bound. Given it, 3 of 3 samples are correct |
| Retrieval | c-023, c-030, c-038 | The `product` naming problem above. Given `product`, every sample is correct |
| Retrieval | c-011, c-040 | The same, but with the right entities Stratum returns the correct answer and omits the count column gold includes, so the scorer still marks it wrong |
| Retrieval | c-045 | Noise entities produced a line-level join (200 rows against 80). With `billing` alone the rows, dates and totals match gold; only the date order differs |
| Generation | c-026 | Sums order totals across order lines, multiplying revenue. Wrong in 2 of 4 attempts even with the right entities |
| Generation | c-028 | Groups by order number without store, though order numbers restart per store. Wrong in every valid attempt |

## Defects this found in the benchmark

- **The legacifier destroys real data.** `_null_marker` in `gauntlet/legacify/chinook.py`
  replaces a non-null value with a marker about 3.5% of the time. For composers that is 74
  tracks whose real composers exist only in the clean database. This contradicts the claim
  that every transformation is reversible, and the reversibility check did not catch it. The
  same function is used for other columns (company, billing state), so other questions may be
  affected. It has not been fixed, because changing the data would invalidate the published
  numbers.
- **The scorer is stricter than the questions.** It is order-sensitive on questions that
  have ties, and it requires a column (a count) that the question did not ask for.

## Limits of this analysis

- **I (Claude) did the classification**, and Claude also wrote the system and the
  benchmark. The correct queries were written by me, which is the weakest part: a query
  written to pass can be made to pass. Four of them failed, which shows they were not
  rubber stamps, but a person should review the 18 rows.
- **One database and one model.** Pagila has not been analysed.
- **Small samples.** The interventions are 2 to 3 runs per question. A third sample was cut
  short by the free tier's daily limit (five rate-limit refusals are excluded, not counted
  as answers). c-026 is intermittent rather than systematic.
- **The boundaries are partly a choice.** The track-table naming error sits between
  excavation and retrieval; it is counted as retrieval because that is where it failed, and
  noted as excavation-rooted.
- **Reproducing needs local files** that are not in git: the version 1 semantic model and
  the raw answer log. The scripts are in `gauntlet/attribution/`.

## One sentence for the submission

> Of Stratum's 18 Chinook failures, I traced 8 to retrieval (5 of them to one naming error
> in the semantic model), 2 to generation, 3 to excavation, 1 to the compiler and 4 to the
> benchmark itself, using a counterfactual (a correct query over the same model) and an
> intervention (re-asking with the correct entities supplied).
