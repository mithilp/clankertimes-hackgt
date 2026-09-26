# McLovin — rubric

Score each hypothesis McLovin sends to a Reporter.

## Hard checks
- [ ] All fields present: `hypothesis`, `why_now`, `who_would_know`, `would_settle_it`, `evidence_so_far`, `accountable_party`
- [ ] `hypothesis` is one sentence that could be true or false (not a topic)
- [ ] `would_settle_it` names specific record types or systems, not "public records"
- [ ] `evidence_so_far` reports distinct origins, not raw item counts
- [ ] Every number is exact

## Judgment checks
- [ ] The hypothesis doesn't treat a registry, ceiling, license or complaint as a transaction or an established fact
- [ ] Items grouped together actually make the same claim, not just share a subject
- [ ] It fits one of the strong shapes: rule plus compliance data, public claim versus own records, category defined by absence, spike against baseline, two datasets that disagree

## Downstream signal
- Share of hypotheses the Reporter confirms, kills with a real contradiction, or parks for lack of evidence. Many quick parks mean `would_settle_it` was wishful
