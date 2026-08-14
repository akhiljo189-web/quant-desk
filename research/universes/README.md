# Universes

`annual.json` is the output of `research/screen.py`, run once per year over
2022–2026. Each entry records the names selected AS OF that date, together
with the market cap, average dollar volume and share-count report date that
put them there — so a selection can be re-derived and argued with rather than
taken on faith.

**Why annual, and not one list.** A universe fixed at the end of the period is
a set of companies that were still mid-cap, still liquid and still listed
afterwards. A universe fixed at the start goes stale as names leave the band.
Re-selecting each year is what the live system would do, and it is the only
version of the run the live system could have reproduced.
`research.replay.walk_forward` takes these and gives each fold the universe
that had been screened by its own start date; a later screen is never visible
to an earlier fold.

**How much it rotates.** 135 unique names across five screens of forty. Nine
appear in four or more; ninety-five appear exactly once. That churn is the
survivorship bias a hand-maintained list hides.

**What it does not fix.** The archive begins where the data plan begins, so
companies delisted before then are invisible here too. This narrows
survivorship; it does not remove it, and the residual points the flattering
way.

Regenerate with `research/screen.py` — do not edit by hand. The commitment is
to the BAND and the selection rule, not to these names.
