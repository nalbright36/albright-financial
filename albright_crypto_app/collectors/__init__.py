"""One collector per source kind (pipeline.scan_sources looks them up in
COLLECTORS). Loaded lazily (PEP 562 __getattr__): importing any submodule -
collectors.utils, collectors.job_boards ... - runs this file, and it must not
import every collector in turn, or a collector importing a sibling (or a
module like templatetags.crypto_links importing a helper) can meet a
partially initialized module. COLLECTORS is built on first use instead.

"manual" sources are filled by the Add form. The income kinds only run once
their terms are checked (Source.runnable, pipeline.scan_sources)."""


def _build():
    from .email_alerts import EmailAlertsCollector
    from .github import GitHubCollector
    from .hn_hiring import HnHiringCollector
    from .job_boards import ArbeitnowCollector, HimalayasCollector, JobicyCollector, RemoteOkCollector, RemotiveCollector
    from .json_api import JsonApiCollector
    from .rss import RssCollector
    return {"github": GitHubCollector, "rss": RssCollector,
            "rss_income": RssCollector, "api_income": JsonApiCollector,
            "remotive": RemotiveCollector, "remoteok": RemoteOkCollector,
            "himalayas": HimalayasCollector, "jobicy": JobicyCollector, "arbeitnow": ArbeitnowCollector,
            "hn_hiring": HnHiringCollector, "email": EmailAlertsCollector}


def __getattr__(name):
    if name == "COLLECTORS":
        collectors = _build()
        globals()["COLLECTORS"] = collectors   # built once; later lookups skip __getattr__
        return collectors
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
