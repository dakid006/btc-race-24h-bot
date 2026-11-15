# btc-race-24h bot: selective EMA trend follower

One file strategy for the 24 hour BTC paper race. Long only, one asset (CBBTC/USDC),
deterministic, no network, no clock, no randomness.

How it trades: track the EMA(50) of closes. If the close is at least 1% above the
EMA and the bot holds nothing, it buys the whole stake (capped by the race weight
limit). If the close falls at least 1% below the EMA and it holds coin, it sells
everything. In between it does nothing. The wide 1% band is the whole point:
every round trip costs about 26 bps in fees plus slippage, so it only trades when
the move is big enough to pay for it, and it sits in cash through a grinding fall
instead of buying the falling knife.

What to run: `python3 main.py` (Python 3.13). It speaks `agent-fun/bars-replay/v1`
on stdin/stdout using the vendored `vendor/makefun_bars.py` shim (the kit SDK).
Nothing else to install.

Checked with the kit's `grade-local` on the dev window and with a local replay
harness over all 1440 dev bars: one clean trade, exit code 0, no rejected orders.

License: MIT. Owner: see `.makefun/freshness.json` and the repo history.
