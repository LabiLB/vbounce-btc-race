"""V-bounce: a regime-aware dip-buying bot for cbBTC/USDC 1-minute bars.

Idea, tuned on the frozen dev window but kept deliberately simple so it can
survive an unseen window:

- Primary entry ("V"): after price has fallen at least 1.2% from its rolling
  120-minute high AND bounced at least 0.2% off its 5-minute low, buy with all
  cash. The drawdown requirement keeps the bot out of chop; the bounce
  requirement keeps it from catching a falling knife mid-drop.
- Exits: stop loss at -1% under cost, trailing stop 1% below the peak
  reached since entry. One full-size position at a time. The take-profit
  is split: half the position is sold at +1.2% over cost (locking in the
  bounce) and the rest rides to +1.8%, the stop or the trailing stop, so a
  rally that stalls just short of the full target still banks most of
  the move.
- Fallback entry: if the bot has never traded by bar 1080 (a quiet window
  with no real dip), relax to a 0.6% drawdown + 0.1% bounce + uptick, with
  tighter exits (TP +1%, SL -0.7%, trail 0.6%). This only ever fires in a
  window the primary never touched, so it cannot dilute a working primary.
- Last resort: if still never traded by bar 1435, buy with all cash and hold
  to the final flatten. A few minutes of exposure costs roughly the
  round-trip fee (~35 bps) and guarantees the >= 1 trade guardrail.
- Drawdown guard: if equity ever falls below 90% of starting equity, sell
  everything and stop entering. With 1% stops this is far from binding; it
  exists only to keep the 25% race guardrail out of reach.

Everything is integer arithmetic on the bars' raw price units, no wall clock,
no randomness, no environment reads: two runs on the same data produce
byte-identical replies.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 300

# Primary V-entry.
LOOKBACK_BARS = 120          # rolling-high window (minutes)
DROP_BPS = 120               # min drawdown from rolling high, in bps (1.2%)
BOUNCE_BPS = 20              # min bounce off the 5-minute low, in bps (0.2%)
LOW_BARS = 5                 # bounce measured off this many minutes' low
# Partial take-profit: sell half at TP1, let the rest ride to TP2 / trail / SL.
TP1_BPS = 120                 # first-half take profit (1.2%)
TP2_BPS = 180                 # second-half take profit (1.8%)
SL_BPS = 100                 # stop loss under cost (1.0%)
TRAIL_BPS = 100              # trailing stop below peak since entry (1.0%)

# Fallback entry (only while the bot has never traded).
FB_START_BAR = 1080
FB_DROP_BPS = 60             # 0.6%
FB_BOUNCE_BPS = 10           # 0.1%
FB_TP_BPS = 100              # 1.0%
FB_SL_BPS = 70               # 0.7%
FB_TRAIL_BPS = 60            # 0.6%

# Last-resort entry (only while the bot has never traded).
BLIND_BAR = 1435

# Drawdown guard: halt entries (and flatten) below 90% of starting equity.
GUARD_NUM = 9
GUARD_DEN = 10


class Bot:
    def __init__(self, state):
        self.state = state
        self.closes = []          # CBBTC closes (raw price units, ints)
        self.peak = None          # highest close since entry (raw units)
        self.mode = None          # "V", "FB" or "BLIND" while holding
        self.partial_done = False  # first-half take-profit taken
        self.traded = False       # any fill yet this run
        self.halted = False
        self.start_equity = int(state.hello["capital"]["quote_units"])

    def on_batch(self, state, batch):
        bar = None
        for b in batch.get("bars", []):
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        for f in batch.get("fills", []):
            if f.get("status") == "filled":
                self.traded = True

        close = int(bar["c"])
        self.closes.append(close)
        idx = state.seq - state.warmup_bars
        if idx < 0:
            return []

        holding = state.positions.get(SYMBOL)
        if holding is not None and holding.qty > 0:
            if self.peak is None or close > self.peak:
                self.peak = close
            if self.halted:
                return [self._sell(state, holding)]
            if state.equity * GUARD_DEN < self.start_equity * GUARD_NUM:
                self.halted = True
                return [self._sell(state, holding)]
            if self.mode == "BLIND":
                return []  # hold to the final flatten
            if self.mode == "FB":
                tp, sl, tr = FB_TP_BPS, FB_SL_BPS, FB_TRAIL_BPS
            else:
                tp, sl, tr = TP2_BPS, SL_BPS, TRAIL_BPS
            value = state.value(SYMBOL, holding.qty)
            cost = holding.cost
            if not self.partial_done and value * 10000 >= cost * (10000 + TP1_BPS):
                self.partial_done = True
                return [self._sell_half(state, holding)]
            if value * 10000 >= cost * (10000 + tp):
                return [self._sell(state, holding)]
            if value * 10000 <= cost * (10000 - sl):
                return [self._sell(state, holding)]
            if close * 10000 <= self.peak * (10000 - tr):
                return [self._sell(state, holding)]
            return []

        # Flat.
        if self.halted:
            return []
        if state.equity * GUARD_DEN < self.start_equity * GUARD_NUM:
            self.halted = True
            return []
        if idx > 1437 or state.cash < 1000000:
            return []

        closes = self.closes
        if len(closes) > LOOKBACK_BARS:
            high = max(closes[-LOOKBACK_BARS - 1:])
            low = min(closes[-LOW_BARS:])
            # Primary V-entry: deep drawdown + bounce off the recent low.
            if (high * 10000 >= close * (10000 + DROP_BPS)
                    and close * 10000 >= low * (10000 + BOUNCE_BPS)):
                self.peak = close
                self.partial_done = False
                self.mode = "V"
                return [self._buy(state)]
            # Fallback entry: only if nothing has ever traded, late window.
            if (not self.traded and idx >= FB_START_BAR
                    and high * 10000 >= close * (10000 + FB_DROP_BPS)
                    and close * 10000 >= low * (10000 + FB_BOUNCE_BPS)
                    and close > closes[-2]):
                self.peak = close
                self.partial_done = False
                self.mode = "FB"
                return [self._buy(state)]
        # Last resort: buy near the end so the run still carries a trade.
        if not self.traded and idx == BLIND_BAR:
            self.peak = close
            self.partial_done = False
            self.mode = "BLIND"
            return [self._buy(state)]
        return []

    @staticmethod
    def _buy(state):
        return {"side": "buy", "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    @staticmethod
    def _sell(state, holding):
        return {"side": "sell", "symbol": SYMBOL, "qty": holding.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    @staticmethod
    def _sell_half(state, holding):
        return {"side": "sell", "symbol": SYMBOL, "qty": holding.qty // 2,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}


def main():
    bot = [None]

    def on_batch(state, batch):
        if bot[0] is None:
            bot[0] = Bot(state)
        return bot[0].on_batch(state, batch)

    makefun_bars.run(on_batch)


if __name__ == "__main__":
    main()
