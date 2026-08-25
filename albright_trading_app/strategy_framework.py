from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """
    Every strategy type subclasses this. The engine (and the
    backtester, later) both call generate_signal() the same way, so
    a strategy's live behavior and its backtested behavior can never
    drift apart from each other.
    """

    def __init__(self, params=None):
        self.params = params or {}

    @abstractmethod
    def generate_signal(self, symbol, bars, sentiment=None):
        """
        bars: list of dicts, oldest-first, each with
        'time', 'open', 'high', 'low', 'close', 'volume' keys —
        the same shape returned by views.get_bars_for_symbol().

        sentiment: dict of Reddit mention data for this symbol, or
        None if no data is available yet. Shape (see
        strategy_filters.get_sentiment_for_symbol):
          mentions_24h, positive_24h, neutral_24h, negative_24h,
          positive_ratio_24h, mentions_7d, positive_7d, ...,
          positive_change_24h_vs_7d_avg, negative_change_24h_vs_7d_avg, ...

        Must return one of: "buy", "sell", "hold".
        """
        raise NotImplementedError


class MovingAverageCrossoverStrategy(BaseStrategy):
    """
    Reference implementation, kept intentionally simple: buy when the
    short-period moving average crosses above the long-period
    average, sell on the reverse crossover, otherwise hold.

    Params:
      short_period (default 10)
      long_period  (default 30)
    """

    def generate_signal(self, symbol, bars, sentiment=None):
        short_period = self.params.get("short_period", 10)
        long_period = self.params.get("long_period", 30)

        if len(bars) < long_period + 1:
            return "hold"  # not enough history to compare yet

        closes = [b["close"] for b in bars]

        def sma(values, period):
            return sum(values[-period:]) / period

        short_now = sma(closes, short_period)
        long_now = sma(closes, long_period)
        short_prev = sma(closes[:-1], short_period)
        long_prev = sma(closes[:-1], long_period)

        crossed_up = short_prev <= long_prev and short_now > long_now
        crossed_down = short_prev >= long_prev and short_now < long_now

        if crossed_up:
            return "buy"
        if crossed_down:
            return "sell"
        return "hold"


class RedditSentimentThresholdStrategy(BaseStrategy):
    """
    Entry based purely on Reddit sentiment strength over the last 24
    hours — ignores price action entirely. Tune what "strong" means
    via params rather than a hardcoded definition:

      min_mentions_24h (default 5)
        Minimum total Reddit mentions today before the reading is
        considered meaningful at all — guards against acting on one
        stray comment.

      min_positive_ratio_24h (default 0.6)
        Positive mentions as a fraction of today's total mentions.
        0.6 means at least 60% of today's mentions were positive.

      min_positive_acceleration_pct (default None — off unless set)
        If set, ALSO requires today's positive mention rate to be up
        at least this % versus the trailing 7-day daily average —
        catches sentiment that's actively building, not just
        steadily positive. Maps to positive_change_24h_vs_7d_avg,
        which the Reddit sentiment command already computes.

    This strategy only ever returns "buy" or "hold" — it has no
    concept of an exit. Positions opened by it MUST be closed via
    this strategy's take_profit_pct / stop_loss_pct / max_hold_days
    settings, or they'll stay open indefinitely.
    """

    def generate_signal(self, symbol, bars, sentiment=None):
        if not sentiment or sentiment["mentions_24h"] == 0:
            return "hold"

        min_mentions = self.params.get("min_mentions_24h", 5)
        min_ratio = self.params.get("min_positive_ratio_24h", 0.6)
        min_acceleration = self.params.get("min_positive_acceleration_pct")

        if sentiment["mentions_24h"] < min_mentions:
            return "hold"

        if sentiment["positive_ratio_24h"] < min_ratio:
            return "hold"

        if min_acceleration is not None:
            acceleration = sentiment.get("positive_change_24h_vs_7d_avg")
            if acceleration is None or acceleration < min_acceleration:
                return "hold"

        return "buy"


def _compute_rsi(closes, period):
    """
    Simple (non-Wilder-smoothed) RSI over the trailing `period` bars.
    Returns None if there isn't enough history yet.
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class RSIThresholdStrategy(BaseStrategy):
    """
    Buy when RSI drops below the oversold threshold, sell when it
    rises above the overbought threshold.

    Params:
      rsi_period (default 14)
      oversold_threshold (default 30)
      overbought_threshold (default 70)
    """

    def generate_signal(self, symbol, bars, sentiment=None):
        period = self.params.get("rsi_period", 14)
        oversold = self.params.get("oversold_threshold", 30)
        overbought = self.params.get("overbought_threshold", 70)

        closes = [b["close"] for b in bars]
        rsi = _compute_rsi(closes, period)
        if rsi is None:
            return "hold"

        if rsi < oversold:
            return "buy"
        if rsi > overbought:
            return "sell"
        return "hold"


class BollingerReversionStrategy(BaseStrategy):
    """
    Buy when price closes below the lower Bollinger Band (potential
    oversold reversion), sell when it closes above the upper band.

    Params:
      period (default 20)
      std_dev (default 2.0) — band width as a multiple of standard deviation
    """

    def generate_signal(self, symbol, bars, sentiment=None):
        period = self.params.get("period", 20)
        std_dev_mult = self.params.get("std_dev", 2.0)

        closes = [b["close"] for b in bars]
        if len(closes) < period:
            return "hold"

        window = closes[-period:]
        mean = sum(window) / period
        variance = sum((c - mean) ** 2 for c in window) / period
        std_dev = variance ** 0.5

        upper_band = mean + std_dev_mult * std_dev
        lower_band = mean - std_dev_mult * std_dev
        current = closes[-1]

        if current < lower_band:
            return "buy"
        if current > upper_band:
            return "sell"
        return "hold"


class PriceBreakoutStrategy(BaseStrategy):
    """
    Buy when price closes above its highest close of the trailing
    lookback window (breakout above resistance), sell when it closes
    below the lowest close of that window (breakdown below support).

    Params:
      breakout_period (default 20)
    """

    def generate_signal(self, symbol, bars, sentiment=None):
        period = self.params.get("breakout_period", 20)

        closes = [b["close"] for b in bars]
        if len(closes) < period + 1:
            return "hold"

        lookback = closes[-(period + 1):-1]  # exclude today's own close
        highest = max(lookback)
        lowest = min(lookback)
        current = closes[-1]

        if current > highest:
            return "buy"
        if current < lowest:
            return "sell"
        return "hold"


# Add every new strategy class here, and add a matching entry to
# Strategy.STRATEGY_TYPE_CHOICES in models.py.
STRATEGY_REGISTRY = {
    "moving_average_crossover": MovingAverageCrossoverStrategy,
    "reddit_sentiment_threshold": RedditSentimentThresholdStrategy,
    "rsi_threshold": RSIThresholdStrategy,
    "bollinger_reversion": BollingerReversionStrategy,
    "price_breakout": PriceBreakoutStrategy,
}