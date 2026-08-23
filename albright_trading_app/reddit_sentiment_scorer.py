from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Finance and WSB-slang terms VADER doesn't know about out of the box,
# scored on VADER's usual -4 to +4 intensity scale. Tune these over
# time based on what you see misclassified in practice.
FINANCE_LEXICON_OVERRIDES = {
    "moon": 3.0,
    "mooning": 3.2,
    "tendies": 2.2,
    "bullish": 2.5,
    "bearish": -2.5,
    "bagholder": -2.5,
    "bagholding": -2.3,
    "rug pull": -3.5,
    "rugpull": -3.5,
    "printing": 1.8,
    "diamond hands": 2.0,
    "paper hands": -1.5,
    "to the moon": 3.0,
    "dump": -2.0,
    "dumping": -2.2,
    "pump": 1.5,
    "pumping": 1.6,
    "short squeeze": 2.0,
    "squeeze": 1.2,
    "overvalued": -1.8,
    "undervalued": 1.8,
    "bankrupt": -3.5,
    "bankruptcy": -3.5,
    "delisted": -3.0,
    "yolo": 1.0,
    "guh": -2.5,  # WSB-specific expression of loss
    "print money": 2.5,
    "dead cat bounce": -1.5,
    "melting up": 2.0,
    "melting down": -2.0,
}


def get_analyzer():
    """
    Returns a VADER SentimentIntensityAnalyzer with the finance
    lexicon merged in. Build one of these per command run and reuse
    it across all texts — don't reconstruct it per call.
    """
    analyzer = SentimentIntensityAnalyzer()
    analyzer.lexicon.update(FINANCE_LEXICON_OVERRIDES)
    return analyzer


def classify_sentiment(text, analyzer):
    """
    Returns 'positive', 'neutral', or 'negative' using VADER's
    standard compound-score thresholds (+/-0.05).
    """
    if not text or not text.strip():
        return "neutral"

    scores = analyzer.polarity_scores(text)
    compound = scores["compound"]

    if compound >= 0.05:
        return "positive"
    elif compound <= -0.05:
        return "negative"
    return "neutral"