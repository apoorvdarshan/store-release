"""store-release: shared App Store Connect and Google Play release tooling.

The ported modules keep each app's constants (paths, bundle ids, tags) as module
globals. `store_release.context.configure()` sets them from the app's
store/store-release.toml; until then they are UNCONFIGURED, and any use fails.
"""
from __future__ import annotations

__version__ = "0.1.0"

NOT_CONFIGURED = "store-release is not configured for an app"


class _Unconfigured:
    """Stands in for an app constant until context.configure() runs.

    Any use (a path join, a comparison, iteration, str(), truthiness) raises
    RuntimeError instead of silently reading another app's value.
    """

    __slots__ = ()

    def _fail(self, *_args, **_kwargs):
        raise RuntimeError(NOT_CONFIGURED)

    def __getattr__(self, _name):
        raise RuntimeError(NOT_CONFIGURED)

    def __repr__(self) -> str:
        return "<store-release: not configured>"

    __truediv__ = __rtruediv__ = __fspath__ = __str__ = __format__ = _fail
    __iter__ = __len__ = __bool__ = __contains__ = __getitem__ = _fail
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = __hash__ = _fail
    __add__ = __radd__ = __mod__ = _fail


UNCONFIGURED = _Unconfigured()
