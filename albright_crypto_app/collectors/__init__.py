from .github import GitHubCollector
from .job_boards import RemoteOkCollector, RemotiveCollector
from .json_api import JsonApiCollector
from .rss import RssCollector

# "manual" sources are filled by the Add form. The income kinds only run once
# their terms are checked (Source.runnable, pipeline.scan_sources).
COLLECTORS = {"github": GitHubCollector, "rss": RssCollector,
              "rss_income": RssCollector, "api_income": JsonApiCollector,
              "remotive": RemotiveCollector, "remoteok": RemoteOkCollector}
