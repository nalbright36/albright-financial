"""manage.py snapshot_strategies - each bot strategy's end-of-day standing
(StrategyDailySnapshot), for its own equity curve on the Grok Bot page
(Alpaca's portfolio history is account-wide only). Run it daily after the
market closes.

For every user with a saved PAPER Alpaca account and at least one bot
strategy: reads the account (read-only - ReadOnlyAlpaca; nothing is placed,
changed or cancelled), attributes every fill to a strategy by its order's
client_order_id prefix, and stores today's row per strategy - plus one for
Unattributed (manual/other): realized P&L to date, unrealized P&L, open
positions, trades closed today.

The first time a strategy (or Unattributed) is snapshotted, its history is
backfilled from past fills - one row per day since its first fill, realized
P&L only (the unrealized P&L of past days can't be rebuilt). --rebuild
redoes that backfill (after a strategy's prefixes change).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from albright_trading_app import strategy_attribution as sa
from albright_trading_app.grok_bot import ReadOnlyAlpaca, _fills_data, _orders_data, _positions_data
from albright_trading_app.models import AlpacaCredentials, BotStrategy, StrategyDailySnapshot


class Command(BaseCommand):
    help = "Store today's P&L snapshot for each Grok Bot strategy (backfilling history on the first run)."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Only this username.")
        parser.add_argument("--rebuild", action="store_true",
                            help="Delete the backfilled history and rebuild it from the fills.")

    def handle(self, *args, user=None, rebuild=False, **options):
        credentials = AlpacaCredentials.objects.select_related("user").filter(user__bot_strategies__isnull=False)
        if user:
            credentials = credentials.filter(user__username=user)
        done = 0
        for creds in credentials.distinct():
            if not creds.is_paper:
                self.stdout.write(f"{creds.user.get_username()}: skipped - a live account is never read.")
                continue
            try:
                self.snapshot(creds, rebuild)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one account failing doesn't stop the others
                self.stderr.write(f"{creds.user.get_username()}: failed - {type(exc).__name__}: {exc}")
        self.stdout.write(f"Snapshotted {done} account{'s' if done != 1 else ''}.")

    def snapshot(self, creds, rebuild):
        user = creds.user
        today = timezone.localdate()
        strategies = list(BotStrategy.objects.filter(user=user))
        refs = sa.refs_from(strategies)
        api = ReadOnlyAlpaca(creds.get_api_key(), creds.get_secret_key())
        orders = _orders_data(api)
        fills = _fills_data(api)
        positions = {p["symbol"]: p for p in _positions_data(api, today)}
        buckets = sa.split_fills(fills["fills"], sa.order_strategies(orders["client_ids"], refs))
        by_pk = {str(s.pk): s for s in strategies}
        for ref in refs + [sa.UNATTRIBUTED_REF]:
            strategy = by_pk.get(ref.key)
            bucket = buckets.get(ref.key, [])
            row = sa.bucket_report(ref, bucket, positions, start=None, today=today)
            rows = StrategyDailySnapshot.objects.filter(user=user, strategy=strategy)
            with transaction.atomic():
                if rebuild:
                    rows.filter(backfilled=True).delete()
                backfilled = 0
                if rebuild or not rows.exists():
                    backfilled = self.backfill(user, strategy, bucket, today)
                StrategyDailySnapshot.objects.update_or_create(
                    user=user, strategy=strategy, date=today,
                    defaults={"realized_pnl": row["realized_to_date"], "unrealized_pnl": row["unrealized"],
                              "open_positions": row["open_count"], "trades_closed": row["trades_closed_today"],
                              "backfilled": False})
            if strategy and not strategy.started_at and row["realizations"]:
                first = min(timezone.localtime(f["time"]).date() for f in bucket if f["time"])
                strategy.started_at = first
                strategy.save(update_fields=["started_at", "updated_at"])
            self.stdout.write(f"{user.get_username()} / {ref.name}: realized ${row['realized_to_date']}, unrealized "
                              f"${row['unrealized']}, {row['open_count']} open, {row['trades_closed_today']} closed "
                              f"today" + (f" (backfilled {backfilled} days)" if backfilled else ""))
        if fills["truncated"] or orders["truncated"]:
            self.stdout.write(f"{user.get_username()}: note - only the most recent fills/orders were read, so the "
                              "oldest history may be incomplete.")

    def backfill(self, user, strategy, fills, today):
        """One row per past day (before today) from the fills; days that already have a row are left alone."""
        existing = set(StrategyDailySnapshot.objects.filter(user=user, strategy=strategy).values_list("date", flat=True))
        rows = [StrategyDailySnapshot(user=user, strategy=strategy, date=day, realized_pnl=realized, unrealized_pnl=None,
                                      open_positions=open_count, trades_closed=closed, backfilled=True)
                for day, realized, open_count, closed in sa.daily_history(fills, today - timedelta(days=1))
                if day not in existing]
        StrategyDailySnapshot.objects.bulk_create(rows)
        return len(rows)
