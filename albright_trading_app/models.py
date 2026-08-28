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
    ("pct_cash", "% of cash"),
]
 
 
class Strategy(models.Model):
    STRATEGY_TYPE_CHOICES = [
        ("moving_average_crossover", "Moving Average Crossover"),
        ("reddit_sentiment_threshold", "Reddit Sentiment Threshold"),
        ("rsi_threshold", "RSI Threshold"),
        ("bollinger_reversion", "Bollinger Band Reversion"),
        ("price_breakout", "Price Breakout"),
    ]
 
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="strategies",
    )
 
    name = models.CharField(max_length=100)
    strategy_type = models.CharField(max_length=50, choices=STRATEGY_TYPE_CHOICES)
    description = models.TextField(blank=True)
 
    symbols = models.CharField(
        max_length=500, blank=True,
        help_text="Optional manual override — comma-separated symbols. Leave blank to use filters instead.",
    )
 
    filter_sectors = models.CharField(max_length=500, blank=True)
    filter_min_price = models.FloatField(null=True, blank=True)
    filter_max_price = models.FloatField(null=True, blank=True)
    filter_min_day_change_pct = models.FloatField(null=True, blank=True)
    filter_max_day_change_pct = models.FloatField(null=True, blank=True)
    filter_min_reddit_mentions_24h = models.PositiveIntegerField(null=True, blank=True)
    filter_min_reddit_positive_ratio = models.FloatField(null=True, blank=True)
 
    parameters = models.JSONField(
        default=dict, blank=True,
        help_text="Built automatically from the strategy-specific form fields — not edited directly.",
    )
 
    position_sizing_method = models.CharField(
        max_length=20, choices=POSITION_SIZING_CHOICES, default="fixed_shares"
    )
    position_sizing_value = models.FloatField(default=1)
 
    take_profit_pct = models.FloatField(null=True, blank=True, help_text="Close once up this %")
    stop_loss_pct = models.FloatField(null=True, blank=True, help_text="Close once down this %")
    trailing_stop_pct = models.FloatField(
        null=True, blank=True,
        help_text="Close if price falls this % from its highest point since entry",
    )

    max_hold_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Force-close after this many days regardless of signal"
    )

    max_daily_entries_per_symbol = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Limit new entries into the same symbol per day. Leave blank for unlimited.",
    )

    filter_min_reddit_positive_vs_negative_ratio = models.FloatField(
        null=True, blank=True,
        help_text="Positive mentions as a fraction of (positive + negative) only — neutral mentions excluded entirely.",
    )

    is_archived = models.BooleanField(
        default=False,
        help_text="Hidden from the main Strategies page but not deleted. Always inactive while archived.",
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

    peak_price = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-entered_at"]

    def __str__(self):
        return f"{self.strategy.name} — {self.side.upper()} {self.quantity} {self.symbol}"

# Options Models
OPTION_DIRECTION_CHOICES = [
    ("bidirectional", "Bidirectional (buy signal \u2192 call, bearish signal \u2192 put)"),
    ("calls_only", "Calls Only"),
    ("puts_only", "Puts Only"),
]

OPTION_STRIKE_METHOD_CHOICES = [
    ("atm", "At-the-Money"),
]
 
OPTION_DTE_CHOICES = [
    (7, "7 days"),
    (30, "30 days"),
    (60, "60 days"),
    (90, "90 days"),
    (120, "120 days"),
]
 
OPTION_POSITION_SIZING_CHOICES = [
    ("fixed_contracts", "Fixed number of contracts"),
    ("fixed_dollar", "Fixed dollar amount"),
    ("pct_buying_power", "% of buying power"),
    ("pct_cash", "% of cash"),
]
 
 
class OptionStrategy(models.Model):
    # Reuses the same entry-signal types as Strategy — the signal
    # logic (moving averages, RSI, Reddit sentiment, etc.) just reads
    # the underlying stock's price/sentiment data and is genuinely
    # asset-class-agnostic, so there's no reason to duplicate
    # strategy_framework.py itself.
    STRATEGY_TYPE_CHOICES = [
        ("moving_average_crossover", "Moving Average Crossover"),
        ("reddit_sentiment_threshold", "Reddit Sentiment Threshold"),
        ("rsi_threshold", "RSI Threshold"),
        ("bollinger_reversion", "Bollinger Band Reversion"),
        ("price_breakout", "Price Breakout"),
    ]
 
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="option_strategies",
    )
 
    name = models.CharField(max_length=100)
    strategy_type = models.CharField(max_length=50, choices=STRATEGY_TYPE_CHOICES)
    description = models.TextField(blank=True)
 
    # ---- Stock universe (of underlyings) ----
    symbols = models.CharField(
        max_length=500, blank=True,
        help_text="Optional manual override — comma-separated underlying symbols. Leave blank to use filters instead.",
    )
    filter_sectors = models.CharField(max_length=500, blank=True)
    filter_min_price = models.FloatField(null=True, blank=True)
    filter_max_price = models.FloatField(null=True, blank=True)
    filter_min_day_change_pct = models.FloatField(null=True, blank=True)
    filter_max_day_change_pct = models.FloatField(null=True, blank=True)
    filter_min_reddit_mentions_24h = models.PositiveIntegerField(null=True, blank=True)
    filter_min_reddit_positive_ratio = models.FloatField(null=True, blank=True)
    filter_min_reddit_positive_vs_negative_ratio = models.FloatField(null=True, blank=True)
 
    # ---- Entry signal ----
    parameters = models.JSONField(default=dict, blank=True)
 
    # ---- Options structure ----
    # Bidirectional by design: a "buy" signal opens a long call, a
    # "sell"/bearish signal opens a long put. Because both signal
    # types OPEN positions rather than one closing the other, an
    # open position can ONLY be closed by risk management below —
    # never by an opposing signal.
    option_direction = models.CharField(
        max_length=20, choices=OPTION_DIRECTION_CHOICES, default="bidirectional"
    )
    option_strike_method = models.CharField(
        max_length=20, choices=OPTION_STRIKE_METHOD_CHOICES, default="atm"
    )
    option_target_dte = models.PositiveIntegerField(
        choices=OPTION_DTE_CHOICES, default=30,
        help_text="Target days-to-expiration when selecting a contract.",
    )
 
    # ---- Position sizing ----
    position_sizing_method = models.CharField(
        max_length=20, choices=OPTION_POSITION_SIZING_CHOICES, default="fixed_contracts"
    )
    position_sizing_value = models.FloatField(default=1)
 
    # ---- Risk management ----
    take_profit_pct = models.FloatField(null=True, blank=True)
    stop_loss_pct = models.FloatField(null=True, blank=True)
    trailing_stop_pct = models.FloatField(null=True, blank=True)
    max_hold_days = models.PositiveIntegerField(null=True, blank=True)
    max_daily_entries_per_symbol = models.PositiveIntegerField(null=True, blank=True)
 
    is_active = models.BooleanField(default=False)
    is_paper = models.BooleanField(default=True)
    is_archived = models.BooleanField(default=False)
 
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_option_strategy_name_per_user")
        ]
 
    @property
    def symbols_list(self):
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
 
    @property
    def filter_sectors_list(self):
        return [s.strip() for s in self.filter_sectors.split(",") if s.strip()]
 
    def __str__(self):
        return f"{self.name} ({self.user.username})"
 
 
class OptionTrade(models.Model):
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]
    OPTION_TYPE_CHOICES = [("call", "Call"), ("put", "Put")]
 
    strategy = models.ForeignKey(OptionStrategy, on_delete=models.CASCADE, related_name="trades")
 
    symbol = models.CharField(max_length=32, help_text="OCC contract symbol, e.g. AAPL260605C00315000")
    underlying_symbol = models.CharField(max_length=10)
    option_type = models.CharField(max_length=4, choices=OPTION_TYPE_CHOICES)
    strike_price = models.FloatField()
    expiration_date = models.DateField()
 
    quantity = models.PositiveIntegerField(help_text="Number of contracts")
    entry_price = models.FloatField(null=True, blank=True, help_text="Premium per share at entry")
    exit_price = models.FloatField(null=True, blank=True, help_text="Premium per share at exit")
    peak_price = models.FloatField(null=True, blank=True)
    realized_pnl = models.FloatField(null=True, blank=True, help_text="Dollar P&L, already x100 for the contract multiplier")
 
    alpaca_order_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open")
 
    entered_at = models.DateTimeField(auto_now_add=True)
    exited_at = models.DateTimeField(null=True, blank=True)
 
    class Meta:
        ordering = ["-entered_at"]
 
    def __str__(self):
        return f"{self.strategy.name} — {self.option_type.upper()} {self.underlying_symbol} ${self.strike_price}"