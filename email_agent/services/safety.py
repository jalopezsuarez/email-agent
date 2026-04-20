"""Hard guarantees that this application cannot send email.

The guarantee is enforced at two levels:

1. OAuth scopes: we never request ``Mail.Send``. Without the scope Microsoft
   Graph rejects any send attempt with ``403 Forbidden`` regardless of the
   client code.

2. Client-side allow-list: ``assert_safe_path`` is invoked from the Graph
   HTTP wrapper before every request. Any path that looks like an outbound
   send endpoint raises ``SendingForbiddenError`` *before* the request is
   ever issued. Tests pin this behaviour.
"""
from __future__ import annotations

import re

# Patterns that indicate an outbound send on Microsoft Graph.
# NOTE: We match a literal slash before "reply"/"forward" so that draft
# endpoints like `/createReply` and `/createForward` are NOT matched.
_FORBIDDEN_PATTERNS = [
    re.compile(r"/sendMail(\b|/|$)", re.IGNORECASE),
    re.compile(r"/messages/[^/]+/send(\b|/|$)", re.IGNORECASE),
    re.compile(r"/reply(All)?\b", re.IGNORECASE),   # /reply and /replyAll (send ops)
    re.compile(r"/forward\b", re.IGNORECASE),       # /forward (send op)
]


class SendingForbiddenError(RuntimeError):
    """Raised whenever the application is about to issue a mail-send request."""


def assert_safe_path(method: str, path: str) -> None:
    """Abort if ``path`` targets a send endpoint.

    Read-only methods on these paths (if any ever existed) would also be
    blocked; that is deliberate — the app has no reason to touch them.
    """
    for pat in _FORBIDDEN_PATTERNS:
        if pat.search(path):
            raise SendingForbiddenError(
                f"Blocked {method} {path}: sending email is disabled by design."
            )
