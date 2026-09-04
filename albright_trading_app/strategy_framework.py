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
    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
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

        relative_strength: dict with the symbol's own today's %
        change vs. its sector's average % change today, or None if
        unavailable. Shape: {"symbol_change_pct", "sector_avg_change_pct", "sector"}.
        Engine-computed once per pass — only strategies that need
        sector context (e.g. SectorRelativeStrengthStrategy) use it.

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

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
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
        Triggers "buy" (a call, for options strategies).

      min_positive_acceleration_pct (default None — off unless set)
        If set, ALSO requires today's positive mention rate to be up
        at least this % versus the trailing 7-day daily average.

      min_negative_ratio_24h (default None — off unless set)
        Negative mentions as a fraction of today's total mentions.
        Triggers "sell" (a put, for options strategies). Leave unset
        to keep this strategy buy-only, matching its original
        behavior — this is purely additive.

      min_negative_acceleration_pct (default None — off unless set)
        Same idea as the positive acceleration check, applied to the
        negative side.

    Buy and sell are evaluated independently — the buy-side logic is
    unchanged from before this option existed. Positions opened by
    this strategy MUST be closed via take_profit_pct / stop_loss_pct
    / trailing_stop_pct / max_hold_days, since sentiment alone has no
    natural exit signal.
    """

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
        if not sentiment or sentiment["mentions_24h"] == 0:
            return "hold"

        min_mentions = self.params.get("min_mentions_24h", 5)
        if sentiment["mentions_24h"] < min_mentions:
            return "hold"

        # ---- Buy side — unchanged from before the negative option existed ----
        min_positive_ratio = self.params.get("min_positive_ratio_24h", 0.6)
        min_positive_acceleration = self.params.get("min_positive_acceleration_pct")

        if sentiment["positive_ratio_24h"] >= min_positive_ratio:
            if min_positive_acceleration is None:
                return "buy"
            acceleration = sentiment.get("positive_change_24h_vs_7d_avg")
            if acceleration is not None and acceleration >= min_positive_acceleration:
                return "buy"

        # ---- Sell side — new, off by default ----
        min_negative_ratio = self.params.get("min_negative_ratio_24h")
        if min_negative_ratio is not None and sentiment.get("negative_ratio_24h", 0) >= min_negative_ratio:
            min_negative_acceleration = self.params.get("min_negative_acceleration_pct")
            if min_negative_acceleration is None:
                return "sell"
            acceleration = sentiment.get("negative_change_24h_vs_7d_avg")
            if acceleration is not None and acceleration >= min_negative_acceleration:
                return "sell"

        return "hold"


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

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
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

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
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

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
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
class VolumeSpikeStrategy(BaseStrategy):
    """
    Buy when today's volume is at least spike_multiplier times the
    average of the trailing lookback_period days AND today's bar
    closed up; sell (mirror) if the spike day closed down. Volume
    alone doesn't tell you direction — today's open-to-close move
    on the spike day supplies that.

    Params:
      lookback_period (default 20)
      spike_multiplier (default 3.0)
    """

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
        lookback = self.params.get("lookback_period", 20)
        multiplier = self.params.get("spike_multiplier", 3.0)

        if len(bars) < lookback + 1:
            return "hold"

        volumes = [b["volume"] for b in bars]
        avg_volume = sum(volumes[-(lookback + 1):-1]) / lookback  # excludes today
        today_volume = volumes[-1]

        if avg_volume <= 0 or today_volume < avg_volume * multiplier:
            return "hold"

        today_change = bars[-1]["close"] - bars[-1]["open"]
        if today_change > 0:
            return "buy"
        if today_change < 0:
            return "sell"
        return "hold"


class SectorRelativeStrengthStrategy(BaseStrategy):
    """
    Buy when a stock is outperforming its own sector's average move
    today by at least min_outperformance_pct; sell when
    underperforming by that much. Requires the engine to supply
    relative_strength context — there's no way to compute a sector
    average from one symbol's own bars alone, so this returns "hold"
    whenever that context isn't available.

    Params:
      min_outperformance_pct (default 2.0)
    """

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
        if not relative_strength:
            return "hold"

        threshold = self.params.get("min_outperformance_pct", 2.0)
        diff = relative_strength["symbol_change_pct"] - relative_strength["sector_avg_change_pct"]

        if diff >= threshold:
            return "buy"
        if diff <= -threshold:
            return "sell"
        return "hold"


class ConfluenceStrategy(BaseStrategy):
    """
    Requires multiple underlying signals to agree before firing.
    Evaluates each configured sub-signal (via the same registry
    every other strategy type uses) against the same bars/sentiment/
    relative_strength, then only returns "buy" or "sell" if at least
    min_agree of them agree on that same direction. Otherwise holds.

    Params:
      signals: list of {"type": <registry key>, "params": {...}}
      min_agree: minimum number of sub-signals that must agree
                 (default: all of them)
    """

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
        sub_signal_configs = self.params.get("signals", [])
        if not sub_signal_configs:
            return "hold"

        min_agree = self.params.get("min_agree") or len(sub_signal_configs)

        results = []
        for config in sub_signal_configs:
            sub_type = config.get("type")
            sub_class = STRATEGY_REGISTRY.get(sub_type)
            if sub_class is None:
                continue
            sub_instance = sub_class(params=config.get("params", {}))
            results.append(
                sub_instance.generate_signal(
                    symbol, bars, sentiment=sentiment, relative_strength=relative_strength, news_sentiment=news_sentiment
                )
            )

        buy_count = results.count("buy")
        sell_count = results.count("sell")

        if buy_count >= min_agree:
            return "buy"
        if sell_count >= min_agree:
            return "sell"
        return "hold"


# Add every new strategy class here, and add a matching entry to
# Strategy.STRATEGY_TYPE_CHOICES / OptionStrategy.STRATEGY_TYPE_CHOICES
# in models.py.
class NewsSentimentThresholdStrategy(BaseStrategy):
    """
    Entry based on news sentiment strength over the last 24 hours —
    parallel to RedditSentimentThresholdStrategy, reading from
    OpenAI-classified news headlines instead of Reddit mentions.
    Positions opened by this strategy MUST rely on take_profit_pct /
    stop_loss_pct / trailing_stop_pct / max_hold_days, since
    sentiment alone has no natural exit signal.

    Params:
      min_mentions_24h (default 3)
      min_positive_ratio_24h (default 0.6) — triggers "buy" (a call)
      min_negative_ratio_24h (default None — off unless set) —
        triggers "sell" (a put). Leave unset to keep this strategy
        buy-only, matching its original behavior.
    """

    def generate_signal(self, symbol, bars, sentiment=None, relative_strength=None, news_sentiment=None):
        if not news_sentiment or news_sentiment["mentions_24h"] == 0:
            return "hold"

        min_mentions = self.params.get("min_mentions_24h", 3)
        if news_sentiment["mentions_24h"] < min_mentions:
            return "hold"

        min_positive_ratio = self.params.get("min_positive_ratio_24h", 0.6)
        if news_sentiment["positive_ratio_24h"] >= min_positive_ratio:
            return "buy"

        min_negative_ratio = self.params.get("min_negative_ratio_24h")
        if min_negative_ratio is not None and news_sentiment.get("negative_ratio_24h", 0) >= min_negative_ratio:
            return "sell"

        return "hold"


# Add every new strategy class here, and add a matching entry to
# Strategy.STRATEGY_TYPE_CHOICES / OptionStrategy.STRATEGY_TYPE_CHOICES
# in models.py.
STRATEGY_REGISTRY = {
    "moving_average_crossover": MovingAverageCrossoverStrategy,
    "reddit_sentiment_threshold": RedditSentimentThresholdStrategy,
    "rsi_threshold": RSIThresholdStrategy,
    "bollinger_reversion": BollingerReversionStrategy,
    "price_breakout": PriceBreakoutStrategy,
    "volume_spike": VolumeSpikeStrategy,
    "sector_relative_strength": SectorRelativeStrengthStrategy,
    "confluence": ConfluenceStrategy,
    "news_sentiment_threshold": NewsSentimentThresholdStrategy,
}