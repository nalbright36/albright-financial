from .github import GitHubCollector
from .email_alerts import EmailAlertsCollector
from .hn_hiring import HnHiringCollector
from .job_boards import ArbeitnowCollector, HimalayasCollector, JobicyCollector, RemoteOkCollector, RemotiveCollector
from .json_api import JsonApiCollector
from .rss import RssCollector

# "manual" sources are filled by the Add form. The income kinds only run once
# their terms are checked (Source.runnable, pipeline.scan_sources).
COLLECTORS = {"github": GitHubCollector, "rss": RssCollector,
              "rss_income": RssCollector, "api_income": JsonApiCollector,
              "remotive": RemotiveCollector, "remoteok": RemoteOkCollector,
              "himalayas": HimalayasCollector, "jobicy": JobicyCollector, "arbeitnow": ArbeitnowCollector,
              "hn_hiring": HnHiringCollector, "email": EmailAlertsCollector}
