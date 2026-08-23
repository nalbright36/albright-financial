def get_sentiment_for_symbol(symbol):
    """
    Returns a Reddit sentiment dict for one symbol, built from
    RedditSentimentSummary — same table the stock detail page reads.
    If the scheduled Reddit command hasn't produced a row for this
    symbol yet, returns an all-zero/None dict rather than raising,
    so sentiment-based strategies can safely treat "no data" as "not
    meaningful" instead of erroring.
    """
    from .models import RedditSentimentSummary
 
    summary = RedditSentimentSummary.objects.filter(symbol=symbol).first()
 
    if summary is None:
        return {
            "mentions_24h": 0, "positive_24h": 0, "neutral_24h": 0, "negative_24h": 0,
            "positive_ratio_24h": 0,
            "mentions_7d": 0, "positive_7d": 0, "neutral_7d": 0, "negative_7d": 0,
            "mentions_30d": 0, "positive_30d": 0, "neutral_30d": 0, "negative_30d": 0,
            "positive_change_24h_vs_7d_avg": None,
            "negative_change_24h_vs_7d_avg": None,
            "positive_change_7d_vs_30d_avg": None,
            "negative_change_7d_vs_30d_avg": None,
        }
 
    positive_ratio_24h = (
        summary.positive_24h / summary.mentions_24h if summary.mentions_24h else 0
    )
 
    return {
        "mentions_24h": summary.mentions_24h,
        "positive_24h": summary.positive_24h,
        "neutral_24h": summary.neutral_24h,
        "negative_24h": summary.negative_24h,
        "positive_ratio_24h": positive_ratio_24h,
        "mentions_7d": summary.mentions_7d,
        "positive_7d": summary.positive_7d,
        "neutral_7d": summary.neutral_7d,
        "negative_7d": summary.negative_7d,
        "mentions_30d": summary.mentions_30d,
        "positive_30d": summary.positive_30d,
        "neutral_30d": summary.neutral_30d,
        "negative_30d": summary.negative_30d,
        "positive_change_24h_vs_7d_avg": summary.positive_change_24h_vs_7d_avg,
        "negative_change_24h_vs_7d_avg": summary.negative_change_24h_vs_7d_avg,
        "positive_change_7d_vs_30d_avg": summary.positive_change_7d_vs_30d_avg,
        "negative_change_7d_vs_30d_avg": summary.negative_change_7d_vs_30d_avg,
    }
 
 
def resolve_strategy_symbols(strategy):
    """
    Returns the list of symbols this strategy should evaluate this
    pass. If the strategy has manual symbols set, those are used
    exactly as entered. Otherwise, the S&P 500 is filtered down using
    the strategy's configured criteria against the same price and
    Reddit sentiment data the Market Scanner displays — so a
    strategy's universe is always as current as the scanner itself.
    """
    if strategy.symbols.strip():
        return strategy.symbols_list
 
    # Imported here (not at module level) to avoid a circular import,
    # since views.py may import from this module too.
    from .views import get_sp500_constituents, get_snapshot_rows, attach_reddit_sentiment
 
    constituents = get_sp500_constituents()
    rows = get_snapshot_rows(constituents)
    rows = attach_reddit_sentiment(rows)
 
    sectors = strategy.filter_sectors_list
    matches = []
 
    for row in rows:
        if sectors and row["sector"] not in sectors:
            continue
        if strategy.filter_min_price is not None and row["price"] < strategy.filter_min_price:
            continue
        if strategy.filter_max_price is not None and row["price"] > strategy.filter_max_price:
            continue
        if strategy.filter_min_day_change_pct is not None and row["change_pct"] < strategy.filter_min_day_change_pct:
            continue
        if strategy.filter_max_day_change_pct is not None and row["change_pct"] > strategy.filter_max_day_change_pct:
            continue
 
        total_mentions = row["reddit_positive"] + row["reddit_neutral"] + row["reddit_negative"]
 
        if strategy.filter_min_reddit_mentions_24h is not None and total_mentions < strategy.filter_min_reddit_mentions_24h:
            continue
 
        if strategy.filter_min_reddit_positive_ratio is not None:
            ratio = (row["reddit_positive"] / total_mentions) if total_mentions else 0
            if ratio < strategy.filter_min_reddit_positive_ratio:
                continue
 
        matches.append(row["symbol"])
 
    return matches