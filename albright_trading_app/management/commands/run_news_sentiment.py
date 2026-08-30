import datetime as dt
import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from openai import OpenAI

from albright_trading_app.models import NewsDailyMentionCount, NewsSentimentSummary
from albright_trading_app.views import get_sp500_constituents

logger = logging.getLogger(__name__)

SYMBOL_CHUNK_SIZE = 50       # symbols per Alpaca news request
CLASSIFICATION_BATCH_SIZE = 25  # articles per OpenAI call — keeps cost down
LOOKBACK_HOURS = 24


class Command(BaseCommand):
    help = "Pulls the last 24h of news for S&P 500 symbols, classifies sentiment via OpenAI (batched), and updates daily/rolling summaries."

    def handle(self, *args, **options):
        constituents = get_sp500_constituents()
        symbols = [c["symbol"] for c in constituents]
        symbol_set = set(symbols)

        try:
            articles = self._fetch_news(symbols)
        except Exception:
            logger.exception("Failed to fetch news")
            self.stdout.write(self.style.ERROR("News fetch failed — aborting run."))
            return

        self.stdout.write(f"Fetched {len(articles)} unique articles for {len(symbols)} symbols.")

        classified = self._classify_articles(articles)
        self.stdout.write(f"Classified {len(classified)} of {len(articles)} articles.")

        counts = self._aggregate_counts(articles, classified, symbol_set)
        self._write_daily_counts(counts)
        self._recompute_summaries(symbols)

        self.stdout.write(self.style.SUCCESS(
            f"News sentiment run complete — {len(counts)} symbols with mentions today."
        ))

    def _fetch_news(self, symbols):
        """
        Fetches the last 24h of news across all S&P 500 symbols,
        chunked to keep each request reasonably sized, and
        deduplicated by article ID — one article commonly gets
        tagged to several tickers at once.

        NOTE: the exact shape of alpaca-py's NewsClient response
        object is the part of this pipeline I'm least certain about
        without live testing — this is written defensively (multiple
        attribute-name fallbacks), but it's worth watching closely on
        the first real run.
        """
        client = NewsClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)

        articles_by_id = {}

        for i in range(0, len(symbols), SYMBOL_CHUNK_SIZE):
            chunk = symbols[i:i + SYMBOL_CHUNK_SIZE]
            try:
                response = client.get_news(
                    NewsRequest(symbols=",".join(chunk), start=cutoff)
                )
            except Exception:
                logger.exception("Failed to fetch news for chunk starting at %d", i)
                continue

            # Defensive extraction — alpaca-py's news response shape
            # varies by version; try the most likely attribute names.
            news_items = (
                getattr(response, "data", None)
                or getattr(response, "news", None)
                or response
            )
            if isinstance(news_items, dict):
                news_items = news_items.get("news", [])

            for article in news_items:
                article_id = getattr(article, "id", None)
                if article_id is None or article_id in articles_by_id:
                    continue

                headline = getattr(article, "headline", "") or ""
                summary = getattr(article, "summary", "") or ""
                related_symbols = getattr(article, "symbols", []) or []

                if not headline:
                    continue

                articles_by_id[article_id] = {
                    "id": article_id,
                    "headline": headline,
                    "summary": summary,
                    "symbols": list(related_symbols),
                }

        return list(articles_by_id.values())

    def _classify_articles(self, articles):
        """
        Returns {article_id: "positive"/"neutral"/"negative"}.
        Classifies in batches of CLASSIFICATION_BATCH_SIZE to control
        OpenAI cost — one call handles many headlines at once instead
        of one call per headline.
        """
        if not settings.OPENAI_API_KEY or not articles:
            return {}

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        results = {}

        for i in range(0, len(articles), CLASSIFICATION_BATCH_SIZE):
            batch = articles[i:i + CLASSIFICATION_BATCH_SIZE]

            numbered = "\n".join(
                f"{j + 1}. {a['headline']} — {a['summary'][:200]}"
                for j, a in enumerate(batch)
            )

            prompt = f"""Classify the sentiment of each of these {len(batch)} financial news headlines as exactly one of: positive, neutral, negative — from the perspective of the primary company/stock the headline concerns.

{numbered}

Respond with ONLY a JSON array of {len(batch)} strings, one per headline in order, like ["positive", "neutral", "negative", ...]. No other text, no markdown formatting."""

            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=600,
                    temperature=0,
                )
                raw = response.choices[0].message.content.strip()
                if raw.startswith("```"):
                    raw = raw.strip("`")
                    if raw.startswith("json"):
                        raw = raw[4:]
                labels = json.loads(raw)

                for article, label in zip(batch, labels):
                    results[article["id"]] = label if label in ("positive", "neutral", "negative") else "neutral"

            except Exception:
                logger.exception("Failed to classify news batch starting at %d — skipping this batch", i)
                continue  # these articles simply won't count toward today's totals

        return results

    def _aggregate_counts(self, articles, classified, symbol_set):
        counts = {}

        for article in articles:
            label = classified.get(article["id"])
            if label is None:
                continue  # classification failed for this article — skip it entirely

            for symbol in article["symbols"]:
                if symbol not in symbol_set:
                    continue
                bucket = counts.setdefault(symbol, {"positive": 0, "neutral": 0, "negative": 0})
                bucket[label] += 1

        return counts

    def _write_daily_counts(self, counts):
        today = dt.datetime.now(dt.timezone.utc).date()

        with transaction.atomic():
            for symbol, bucket in counts.items():
                total = bucket["positive"] + bucket["neutral"] + bucket["negative"]
                NewsDailyMentionCount.objects.update_or_create(
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
        today = dt.datetime.now(dt.timezone.utc).date()
        window_7d_start = today - dt.timedelta(days=6)
        window_30d_start = today - dt.timedelta(days=29)

        for symbol in all_symbols:
            today_row = NewsDailyMentionCount.objects.filter(symbol=symbol, date=today).first()

            window_7d = NewsDailyMentionCount.objects.filter(
                symbol=symbol, date__gte=window_7d_start, date__lte=today
            ).aggregate(
                positive=Sum("positive_count"), neutral=Sum("neutral_count"),
                negative=Sum("negative_count"), total=Sum("total_count"),
            )
            window_30d = NewsDailyMentionCount.objects.filter(
                symbol=symbol, date__gte=window_30d_start, date__lte=today
            ).aggregate(
                positive=Sum("positive_count"), neutral=Sum("neutral_count"),
                negative=Sum("negative_count"), total=Sum("total_count"),
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

            if mentions_30d == 0:
                continue

            NewsSentimentSummary.objects.update_or_create(
                symbol=symbol,
                defaults={
                    "mentions_24h": mentions_24h, "positive_24h": positive_24h,
                    "neutral_24h": neutral_24h, "negative_24h": negative_24h,
                    "mentions_7d": mentions_7d, "positive_7d": positive_7d,
                    "neutral_7d": neutral_7d, "negative_7d": negative_7d,
                    "mentions_30d": mentions_30d, "positive_30d": positive_30d,
                    "neutral_30d": neutral_30d, "negative_30d": negative_30d,
                    "positive_change_24h_vs_7d_avg": self._pct_change_vs_daily_avg(positive_24h, positive_7d, 7),
                    "negative_change_24h_vs_7d_avg": self._pct_change_vs_daily_avg(negative_24h, negative_7d, 7),
                    "positive_change_7d_vs_30d_avg": self._pct_change_vs_daily_avg(
                        positive_7d / 7 if positive_7d else 0, positive_30d, 30
                    ),
                    "negative_change_7d_vs_30d_avg": self._pct_change_vs_daily_avg(
                        negative_7d / 7 if negative_7d else 0, negative_30d, 30
                    ),
                },
            )

    @staticmethod
    def _pct_change_vs_daily_avg(recent_value, window_total, window_days):
        daily_avg = window_total / window_days if window_days else 0
        if daily_avg == 0:
            return None
        return ((recent_value - daily_avg) / daily_avg) * 100