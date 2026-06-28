# Position Lifecycle Rules

Scanning is not trading. A scan result is only evidence for a candidate opportunity. It is not a buy command, sell command, close command, or reverse command.

The correct flow is:

1. Market scan.
2. Candidate symbol.
3. Trade thesis.
4. Trade plan.
5. Entry.
6. Position lifecycle management.
7. Hold, reduce, add, or exit.
8. Review and learning.

Every strategy must define these lifecycle states:

- `WAITING`: waiting for a valid opportunity.
- `ENTRY_READY`: entry conditions are satisfied.
- `OPENED`: position is opened.
- `PROTECTING`: initial protection period; verify whether the thesis is alive.
- `TREND_HOLD`: trend or continuation thesis is still valid.
- `SCALE_IN`: add only when proven safe; currently disabled unless research proves it.
- `SCALE_OUT`: partial profit-taking or risk reduction.
- `DEFENSIVE`: risk is reduced or tightened, but the core position is not automatically closed.
- `EXIT_READY`: exit conditions are satisfied.
- `CLOSED`: position is finished and ready for review.

Every strategy must output:

- `hold_logic`: when to continue holding.
- `reduce_logic`: when to reduce but not fully exit.
- `add_logic`: when adding is allowed. Current default is max_adds=0.
- `exit_logic`: when the core position must be closed.
- `time_stop`: what to do if the trade does not develop.
- `review_metrics`: at least MFE, MAE, R_multiple, max_drawdown, hold_time.

Core rule:

Do not close the core position because of a minor reverse signal alone. Exit only when the trade thesis is invalidated, risk limits fire, time stop fires, TP2 is hit, or market structure breaks.

Review must answer:

- Did we exit too early?
- Did we hold a loser too long?
- Was the stop too tight?
- Was the TP too close?
- Did the trade have enough MFE to justify a different hold rule?
- Did MAE show the entry was too late or too early?
