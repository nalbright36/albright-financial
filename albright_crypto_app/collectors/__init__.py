from .github import GitHubCollector
from .rss import RssCollector

COLLECTORS = {"github": GitHubCollector, "rss": RssCollector}  # "manual" sources are filled by the Add form
