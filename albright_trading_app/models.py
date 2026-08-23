from django.conf import settings
from django.db import models
from django.contrib.auth.models import User

from .crypto_utils import get_fernet

# Create your models here.
class UserProfileInfo(models.Model):

    user = models.OneToOneField(User,on_delete=models.CASCADE)

    def __str__(self):
        return self.user.username

class InvestorProfile(models.Model):

    username = models.CharField(max_length=75)
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    email = models.CharField(max_length=50)
    date_of_birth = models.DateField()
    expected_retirement_date = models.DateField()
    risk_tolerance = models.CharField(max_length=50)
    marital_status = models.CharField(max_length=50)
    dependents_count = models.IntegerField()
    home_street_address = models.CharField(max_length=150)
    home_state = models.CharField(max_length=50)
    home_city = models.CharField(max_length=50)
    home_zip = models.CharField(max_length=50)
    employment_status = models.CharField(max_length=50)
    goal_type = models.CharField(max_length=50)
    maximum_tolerable_drawdown = models.FloatField()
    monthly_contribution_amount = models.FloatField()
    liquid_assets = models.FloatField()
    debt = models.FloatField()

class RedditDailyMentionCount(models.Model):
    """
    One row per (symbol, date). This is the raw daily ledger the
    scheduled command writes to every morning for the previous day's
    activity. 7-day and 30-day windows are built by summing these
    rows rather than re-querying Reddit for historical data.
    """
    symbol = models.CharField(max_length=10, db_index=True)
    date = models.DateField(db_index=True)
 
    positive_count = models.PositiveIntegerField(default=0)
    neutral_count = models.PositiveIntegerField(default=0)
    negative_count = models.PositiveIntegerField(default=0)
    total_count = models.PositiveIntegerField(default=0)
 
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["symbol", "date"], name="unique_symbol_per_day"
            )
        ]
        indexes = [
            models.Index(fields=["symbol", "date"]),
        ]
        ordering = ["-date", "symbol"]
 
    def __str__(self):
        return f"{self.symbol} — {self.date} ({self.total_count} mentions)"
 
 
class RedditSentimentSummary(models.Model):
    """
    One row per symbol, fully recomputed on every command run from
    RedditDailyMentionCount. This is the table the market scanner
    reads from — it never needs to touch Reddit or the daily ledger
    directly.
    """
    symbol = models.CharField(max_length=10, unique=True, db_index=True)
    computed_at = models.DateTimeField(auto_now=True)
 
    # Raw window totals
    mentions_24h = models.PositiveIntegerField(default=0)
    positive_24h = models.PositiveIntegerField(default=0)
    neutral_24h = models.PositiveIntegerField(default=0)
    negative_24h = models.PositiveIntegerField(default=0)
 
    mentions_7d = models.PositiveIntegerField(default=0)
    positive_7d = models.PositiveIntegerField(default=0)
    neutral_7d = models.PositiveIntegerField(default=0)
    negative_7d = models.PositiveIntegerField(default=0)
 
    mentions_30d = models.PositiveIntegerField(default=0)
    positive_30d = models.PositiveIntegerField(default=0)
    neutral_30d = models.PositiveIntegerField(default=0)
    negative_30d = models.PositiveIntegerField(default=0)
 
    # Trend comparisons: how does the most recent period's daily rate
    # compare to the longer-window daily average? Positive values
    # mean accelerating, negative means cooling off. Stored as a
    # percentage (e.g. 45.0 means +45%).
    positive_change_24h_vs_7d_avg = models.FloatField(null=True, blank=True)
    negative_change_24h_vs_7d_avg = models.FloatField(null=True, blank=True)
    positive_change_7d_vs_30d_avg = models.FloatField(null=True, blank=True)
    negative_change_7d_vs_30d_avg = models.FloatField(null=True, blank=True)
 
    class Meta:
        ordering = ["-mentions_24h"]
 
    def __str__(self):
        return f"{self.symbol} sentiment summary (as of {self.computed_at:%Y-%m-%d})"

class AlpacaCredentials(models.Model):
    """
    One row per user, storing their personal Alpaca API key/secret so
    the Trading Account dashboard shows *their* account rather than a
    single shared one. Keys are encrypted at rest with Fernet — never
    stored, logged, or displayed in plaintext after initial entry.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="alpaca_credentials",
    )
    encrypted_api_key = models.TextField()
    encrypted_secret_key = models.TextField()
    is_paper = models.BooleanField(default=True)

    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_credentials(self, api_key, secret_key):
        fernet = get_fernet()
        self.encrypted_api_key = fernet.encrypt(api_key.encode()).decode()
        self.encrypted_secret_key = fernet.encrypt(secret_key.encode()).decode()

    def get_api_key(self):
        return get_fernet().decrypt(self.encrypted_api_key.encode()).decode()

    def get_secret_key(self):
        return get_fernet().decrypt(self.encrypted_secret_key.encode()).decode()

    def __str__(self):
        return f"Alpaca credentials for {self.user.username}"

POSITION_SIZING_CHOICES = [
    ("fixed_shares", "Fixed number of shares"),
    ("fixed_dollar", "Fixed dollar amount"),
    ("pct_buying_power", "% of buying power"),
]
 
 
class Strategy(models.Model):
    STRATEGY_TYPE_CHOICES = [
        ("moving_average_crossover", "Moving Average Crossover"),
        ("reddit_sentiment_threshold", "Reddit Sentiment Threshold"),
    ]
 
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="strategies",
    )
 
    name = models.CharField(max_length=100)
    strategy_type = models.CharField(max_length=50, choices=STRATEGY_TYPE_CHOICES)
    description = models.TextField(blank=True)
 
    # ---- Stock universe: manual symbols OR filter criteria ----
    # If `symbols` is set, it's used as-is and every filter below is
    # ignored. Leave it blank to have the engine dynamically resolve
    # the eligible S&P 500 universe from the filters every pass.
    symbols = models.CharField(
        max_length=500, blank=True,
        help_text="Optional manual override — comma-separated symbols. Leave blank to use filters instead.",
    )
 
    filter_sectors = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated GICS sectors to include. Leave blank for all sectors.",
    )
    filter_min_price = models.FloatField(null=True, blank=True)
    filter_max_price = models.FloatField(null=True, blank=True)
    filter_min_day_change_pct = models.FloatField(
        null=True, blank=True, help_text="Only include stocks up at least this % today"
    )
    filter_max_day_change_pct = models.FloatField(
        null=True, blank=True, help_text="Only include stocks up at most this % today"
    )
    filter_min_reddit_mentions_24h = models.PositiveIntegerField(null=True, blank=True)
    filter_min_reddit_positive_ratio = models.FloatField(
        null=True, blank=True,
        help_text="0.0–1.0 — require at least this fraction of today's Reddit mentions to be positive",
    )
 
    # ---- Entry signal ----
    parameters = models.JSONField(
        default=dict, blank=True,
        help_text='Strategy-specific config, e.g. {"short_period": 10, "long_period": 30}',
    )
 
    # ---- Position sizing ----
    position_sizing_method = models.CharField(
        max_length=20, choices=POSITION_SIZING_CHOICES, default="fixed_shares"
    )
    position_sizing_value = models.FloatField(
        default=1,
        help_text="Meaning depends on method: share count, dollar amount, or percent (e.g. 5 = 5%)",
    )
 
    # ---- Risk management ----
    take_profit_pct = models.FloatField(null=True, blank=True, help_text="Close once up this %")
    stop_loss_pct = models.FloatField(null=True, blank=True, help_text="Close once down this %")
    max_hold_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Force-close after this many days regardless of signal"
    )
 
    is_active = models.BooleanField(default=False)
    is_paper = models.BooleanField(default=True)
 
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_strategy_name_per_user")
        ]
 
    @property
    def symbols_list(self):
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
 
    @property
    def filter_sectors_list(self):
        return [s.strip() for s in self.filter_sectors.split(",") if s.strip()]
 
    def __str__(self):
        return f"{self.name} ({self.user.username})"


class Trade(models.Model):
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]

    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, related_name="trades")
    symbol = models.CharField(max_length=10)
    side = models.CharField(max_length=4)  # "buy" or "sell"
    quantity = models.PositiveIntegerField()

    entry_price = models.FloatField(null=True, blank=True)
    exit_price = models.FloatField(null=True, blank=True)
    realized_pnl = models.FloatField(null=True, blank=True)

    alpaca_order_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open")

    entered_at = models.DateTimeField(auto_now_add=True)
    exited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-entered_at"]

    def __str__(self):
        return f"{self.strategy.name} — {self.side.upper()} {self.quantity} {self.symbol}"