"""
conftest.py
-----------
Shared pytest fixtures and configuration.

Phase 1: ensures that the project root is on sys.path so that
`utils`, `github`, and `review` packages resolve correctly regardless
of where pytest is invoked from.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is in sys.path.
_PROJECT_ROOT = Path(__file__).parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
