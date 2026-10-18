"""Gemini API key pool with automatic rotation on quota exhaustion.

API Keys v2 (2026-10-18): the user can register up to 5 Gemini keys
(each from a different Google account, each with its own free quota).
Every Gemini call site goes through :class:`GeminiKeyPool`, which tries
keys in order; when a key is quota-exhausted (HTTP 429,
RESOURCE_EXHAUSTED, or a "quota" mention in the error), it is marked
exhausted and the next key is tried. Non-quota errors raise immediately.
When every key is exhausted, a fatal Burmese error tells the user to add
a key from another Google account.

Keys are NEVER logged or stored here — they travel as plain strings from
the caller's BYOK UI, exactly like the single-key flow before.
"""

from typing import Any, Callable, Dict, List, Optional, Union

ALL_KEYS_EXHAUSTED_MSG = (
    "Gemini key အားလုံး quota ကုန်နေပါတယ် — "
    "တခြား Google account မှ key အသစ် ထည့်ပါ"
)

# Tokens that identify a QUOTA problem (not a bad request, not a bad
# key). 401/403 (bad key) and 400 (bad request) are deliberately absent:
# retrying another key cannot fix those, so they must raise immediately.
_QUOTA_TOKENS = (
    "429",
    "resource_exhausted",
    "quota exceeded",
    "quota_exhausted",
    "quota has been exceeded",
    "exceeds quota",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
)


def normalize_keys(user_api_key: Union[str, List[str], None]) -> List[str]:
    """Accept a single key string or a list; return non-empty keys."""
    if user_api_key is None:
        return []
    if isinstance(user_api_key, str):
        keys = [user_api_key]
    else:
        try:
            keys = list(user_api_key)
        except TypeError:
            keys = []
    return [k.strip() for k in keys if isinstance(k, str) and k.strip()]


def is_quota_error(exc: BaseException) -> bool:
    """True when the error looks like quota exhaustion / rate limiting."""
    try:
        msg = str(exc).lower()
    except Exception:
        return False
    if not msg:
        return False
    # An explicit HTTP status attribute (set by our own wrappers) wins.
    status = getattr(exc, "http_status", None)
    if status is not None:
        try:
            if int(status) == 429:
                return True
        except (TypeError, ValueError):
            pass
    return any(tok in msg for tok in _QUOTA_TOKENS)


class QuotaExhaustedError(Exception):
    """Raised when a single Gemini key's quota is exhausted.

    The pool catches this (and any error matching :func:`is_quota_error`)
    and rotates to the next key. Anything else propagates immediately.
    """


class GeminiKeyPool:
    """Rotating pool of one run's Gemini keys.

    ``on_exhausted`` is called with the 1-based index of each key the
    moment it is marked exhausted, so the UI can warn
    ("Key 1 quota ကုန်သွားလို့ Key 2 နဲ့ ဆက်လုပ်နေပါတယ်").
    """

    def __init__(self,
                 keys: Union[str, List[str], None],
                 on_exhausted: Optional[Callable[[int], None]] = None):
        self.keys: List[str] = normalize_keys(keys)
        self.exhausted: set = set()
        self.on_exhausted = on_exhausted

    def __bool__(self) -> bool:
        return bool(self.keys)

    def __len__(self) -> int:
        return len(self.keys)

    @property
    def live_keys(self) -> List[str]:
        """Keys not yet marked exhausted (in original order)."""
        return [k for i, k in enumerate(self.keys) if i not in self.exhausted]

    def first_key(self) -> str:
        """First live key (for call sites that need one key, no retry)."""
        live = self.live_keys
        if not live:
            raise QuotaExhaustedError(ALL_KEYS_EXHAUSTED_MSG)
        return live[0]

    def run(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Call ``fn(key, *args, **kwargs)``; rotate on quota errors.

        Non-quota errors raise immediately without trying the next key.
        When every key is exhausted, raises QuotaExhaustedError with the
        exact Burmese fatal message.
        """
        last_exc: Optional[BaseException] = None
        for i, key in enumerate(self.keys):
            if i in self.exhausted:
                continue
            try:
                return fn(key, *args, **kwargs)
            except QuotaExhaustedError as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 - classified below
                if not is_quota_error(exc):
                    raise
                last_exc = exc
            # This key is done — mark it and move on.
            self.exhausted.add(i)
            if self.on_exhausted is not None:
                try:
                    self.on_exhausted(i + 1)
                except Exception:
                    pass
        raise QuotaExhaustedError(ALL_KEYS_EXHAUSTED_MSG) from last_exc
