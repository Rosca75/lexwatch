"""Web app package.

Authlib (imported by ``web.auth``) emits ``AuthlibDeprecationWarning: The httpx module is
deprecated; please use httpx2`` on import, and ``authlib.deprecate`` forces its category to
"always" shown. lexwatch uses httpx on purpose (see ``pyproject.toml``), so that one message is
filtered here, before any submodule imports Authlib. Other warnings stay visible.
"""

import warnings

try:
    from authlib.deprecate import AuthlibDeprecationWarning
except ImportError:  # pragma: no cover - authlib is a runtime dependency
    pass
else:
    warnings.filterwarnings(
        "ignore",
        message=r"The httpx module is deprecated",
        category=AuthlibDeprecationWarning,
    )
