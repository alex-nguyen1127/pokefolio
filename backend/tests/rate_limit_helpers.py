"""Keep non-rate-limit tests independent while exercising the real middleware."""

from itertools import count
from unittest.mock import patch

from app.core.rate_limit import AuthRateLimiter


def isolate_auth_rate_limit(test, app):
    # Each request is in a fresh window. Dedicated limiter tests control time
    # within a window; auth tests can retain all their existing validation cases.
    limiter = AuthRateLimiter(clock=count(step=61).__next__)
    override = patch.object(app.state, "auth_rate_limiter", limiter)
    override.start()
    test.addCleanup(override.stop)
