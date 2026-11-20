"""Request timing: total time, number of DB queries and time spent in the DB.

On for every request when DEBUG is on (logged to "albright.timing"), and for a superuser's request with ?_timing=1
or an "X-Timing: 1" header. The numbers come back as a Server-Timing header (the browser's Network panel shows it:
total, db and the query count) and as X-Timing. Off otherwise: nothing is wrapped and nothing is added.
"""
import logging
import time

from django.conf import settings
from django.db import connection

log = logging.getLogger("albright.timing")


class _Counter:
    def __init__(self):
        self.queries = 0
        self.db = 0.0

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.db += time.perf_counter() - start
            self.queries += 1


def wanted(request):
    if settings.DEBUG:
        return True
    if request.GET.get("_timing") != "1" and request.headers.get("X-Timing") != "1":
        return False
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_superuser)


class TimingMiddleware:
    """Placed after AuthenticationMiddleware (it needs request.user to know a superuser)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not wanted(request):
            return self.get_response(request)
        counter = _Counter()
        start = time.perf_counter()
        with connection.execute_wrapper(counter):
            response = self.get_response(request)
        total = (time.perf_counter() - start) * 1000
        db = counter.db * 1000
        response["Server-Timing"] = f'total;dur={total:.1f}, db;dur={db:.1f};desc="{counter.queries} queries"'
        response["X-Timing"] = f"total={total:.1f}ms db={db:.1f}ms queries={counter.queries}"
        log.info("%s %s %s total=%.1fms db=%.1fms queries=%d", request.method, request.path, response.status_code,
                 total, db, counter.queries)
        return response
