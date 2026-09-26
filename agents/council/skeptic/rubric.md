# Skeptic — rubric

## Seeded-error catch rate (the main metric)
Plant each error from [examples/bad/seeded-errors.md](examples/bad/seeded-errors.md) into a known-good draft and record whether the skeptic sends it back **for that reason**. Track catch rate per error type.

## False-block rate
Run known-good drafts with no planted errors. Every block is a false positive unless the reason is real. A skeptic that blocks everything is as useless as one that blocks nothing.

## Per-verdict checks
- [ ] Every objection quotes the sentence it's about
- [ ] Every objection says what would fix it
- [ ] No objections about style alone
