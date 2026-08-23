import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone

import praw
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from albright_trading_app.models import RedditDailyMentionCount, RedditSentimentSummary
from albright_trading_app.reddit_ticker_matcher import build_ticker_patterns, find_tickers
from albright_trading_app.reddit_sentiment_scorer import get_analyzer, classify_sentiment

# NOTE: fix the import above to match your actual app name/module
# layout before running — see the note at the bottom of the chat
# response for exactly what to adjust.

logger = logging.getLogger(__name__)

TARGET_SUBREDDITS = ["wallstreetbets", "stocks", "investing", "StockMarket"]
LOOKBACK_HOURS = 24
POST_LISTING_LIMIT = 500  # per subreddit, per run — adjust based on rate-limit headroom


class Command(BaseCommand):
    help = "Pulls Reddit mentions of S&P 500 tickers over the last 24h, scores sentiment, and updates rolling summaries."

    def handle(self, *args, **options):
        from albright_trading_app.views import get_sp500_constituents

        constituents = get_sp500_constituents()
        symbols = [c["symbol"] for c in constituents]
        cashtag_pattern, bare_pattern = build_ticker_patterns(symbols)
        analyzer = get_analyzer()

        counts = self._collect_mentions(cashtag_pattern, bare_pattern, analyzer)
        self._write_daily_counts(counts)
        self._recompute_summaries(symbols)

        self.stdout.write(self.style.SUCCESS(
            f"Reddit sentiment run complete — {len(counts)} symbols mentioned today."
        ))

    def _reddit_client(self):
        return praw.Reddit(
            client_id=settings.REDDIT_CLIENT_ID,
            client_secret=settings.REDDIT_CLIENT_SECRET,
            user_agent=settings.REDDIT_USER_AGENT,
        )

    def _collect_mentions(self, cashtag_pattern, bare_pattern, analyzer):
        """
        Walks each target subreddit's new posts (and their top-level
        comments) from the last 24h, matches tickers, scores
        sentiment per matched text, and returns a dict:
        {symbol: {"positive": n, "neutral": n, "negative": n}}
        """
        reddit = self._reddit_client()
        cutoff = datetime.now(dt_timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        counts = {}

        def record(symbol, sentiment):
            bucket = counts.setdefault(
                symbol, {"positive": 0, "neutral": 0, "negative": 0}
            )
            bucket[sentiment] += 1

        for subreddit_name in TARGET_SUBREDDITS:
            self.stdout.write(f"Scanning r/{subreddit_name}...")
            subreddit = reddit.subreddit(subreddit_name)

            try:
                for submission in subreddit.new(limit=POST_LISTING_LIMIT):
                    post_time = datetime.fromtimestamp(
                        submission.created_utc, tz=dt_timezone.utc
                    )
                    if post_time < cutoff:
                        # subreddit.new() is reverse-chronological, so
                        # once we're past the window we can stop early.
                        break

                    post_text = f"{submission.title} {submission.selftext or ''}"
                    for symbol in find_tickers(post_text, cashtag_pattern, bare_pattern):
                        sentiment = classify_sentiment(post_text, analyzer)
                        record(symbol, sentiment)

                    # Top-level comments only — going deeper multiplies
                    # API calls fast for little added signal.
                    submission.comments.replace_more(limit=0)
                    for comment in submission.comments:
                        comment_time = datetime.fromtimestamp(
                            comment.created_utc, tz=dt_timezone.utc
                        )
                        if comment_time < cutoff:
                            continue

                        for symbol in find_tickers(comment.body, cashtag_pattern, bare_pattern):
                            sentiment = classify_sentiment(comment.body, analyzer)
                            record(symbol, sentiment)

            except Exception:
                # One subreddit failing (auth hiccup, deleted post,
                # etc.) shouldn't take down the whole run.
                logger.exception("Failed while scanning r/%s", subreddit_name)
                continue

            time.sleep(1)  # small courtesy pause between subreddits

        return counts

    def _write_daily_counts(self, counts):
        """
        Upserts today's RedditDailyMentionCount row for every symbol
        that was mentioned. Symbols with zero mentions today simply
        get no row — the rolling sums below treat "no row" as zero.
        """
        today = datetime.now(dt_timezone.utc).date()

        with transaction.atomic():
            for symbol, bucket in counts.items():
                total = bucket["positive"] + bucket["neutral"] + bucket["negative"]
                RedditDailyMentionCount.objects.update_or_create(
                    symbol=symbol,
                    date=today,
                    defaults={
                        "positive_count": bucket["positive"],
                        "neutral_count": bucket["neutral"],
                        "negative_count": bucket["negative"],
                        "total_count": total,
                    },
                )

    def _recompute_summaries(self, all_symbols):
        """
        Rebuilds RedditSentimentSummary for every S&P 500 symbol from
        the daily ledger — 24h is today's row, 7d/30d are rolling
        sums over the trailing window.
        """
        today = datetime.now(dt_timezone.utc).date()
        window_7d_start = today - timedelta(days=6)   # inclusive 7-day window
        window_30d_start = today - timedelta(days=29)  # inclusive 30-day window

        for symbol in all_symbols:
            today_row = RedditDailyMentionCount.objects.filter(
                symbol=symbol, date=today
            ).first()

            window_7d = RedditDailyMentionCount.objects.filter(
                symbol=symbol, date__gte=window_7d_start, date__lte=today
            ).aggregate(
                positive=Sum("positive_count"),
                neutral=Sum("neutral_count"),
                negative=Sum("negative_count"),
                total=Sum("total_count"),
            )

            window_30d = RedditDailyMentionCount.objects.filter(
                symbol=symbol, date__gte=window_30d_start, date__lte=today
            ).aggregate(
                positive=Sum("positive_count"),
                neutral=Sum("neutral_count"),
                negative=Sum("negative_count"),
                total=Sum("total_count"),
            )

            positive_24h = today_row.positive_count if today_row else 0
            neutral_24h = today_row.neutral_count if today_row else 0
            negative_24h = today_row.negative_count if today_row else 0
            mentions_24h = today_row.total_count if today_row else 0

            positive_7d = window_7d["positive"] or 0
            neutral_7d = window_7d["neutral"] or 0
            negative_7d = window_7d["negative"] or 0
            mentions_7d = window_7d["total"] or 0

            positive_30d = window_30d["positive"] or 0
            neutral_30d = window_30d["neutral"] or 0
            negative_30d = window_30d["negative"] or 0
            mentions_30d = window_30d["total"] or 0

            # Skip symbols with no activity in any window — no point
            # keeping a stale all-zero summary row cluttering results.
            if mentions_30d == 0:
                continue

            positive_change_24h_vs_7d = self._pct_change_vs_daily_avg(
                positive_24h, positive_7d, 7
            )
            negative_change_24h_vs_7d = self._pct_change_vs_daily_avg(
                negative_24h, negative_7d, 7
            )
            positive_change_7d_vs_30d = self._pct_change_vs_daily_avg(
                positive_7d / 7 if positive_7d else 0, positive_30d, 30
            )
            negative_change_7d_vs_30d = self._pct_change_vs_daily_avg(
                negative_7d / 7 if negative_7d else 0, negative_30d, 30
            )

            RedditSentimentSummary.objects.update_or_create(
                symbol=symbol,
                defaults={
                    "mentions_24h": mentions_24h,
                    "positive_24h": positive_24h,
                    "neutral_24h": neutral_24h,
                    "negative_24h": negative_24h,
                    "mentions_7d": mentions_7d,
                    "positive_7d": positive_7d,
                    "neutral_7d": neutral_7d,
                    "negative_7d": negative_7d,
                    "mentions_30d": mentions_30d,
                    "positive_30d": positive_30d,
                    "neutral_30d": neutral_30d,
                    "negative_30d": negative_30d,
                    "positive_change_24h_vs_7d_avg": positive_change_24h_vs_7d,
                    "negative_change_24h_vs_7d_avg": negative_change_24h_vs_7d,
                    "positive_change_7d_vs_30d_avg": positive_change_7d_vs_30d,
                    "negative_change_7d_vs_30d_avg": negative_change_7d_vs_30d,
                },
            )

    @staticmethod
    def _pct_change_vs_daily_avg(recent_value, window_total, window_days):
        """
        Compares a recent daily-rate value against the daily average
        of a longer window, as a percentage change. Returns None if
        there's no baseline to compare against (avoids divide-by-zero
        and avoids implying a trend from nothing).
        """
        daily_avg = window_total / window_days if window_days else 0
        if daily_avg == 0:
            return None
        return ((recent_value - daily_avg) / daily_avg) * 100