#!/usr/bin/env python3
"""Backward-compatible launcher for the local workbench HTTP server."""

import sys

from workbench import http_api as _http_api


if __name__ == "__main__":
    _http_api.main()
else:
    # Preserve import and monkeypatch behavior for existing tests and scripts.
    sys.modules[__name__] = _http_api
