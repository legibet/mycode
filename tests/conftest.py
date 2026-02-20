"""Pytest configuration for mycode tests."""

import sys
from pathlib import Path

# Add app directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest_plugins = ["pytest_asyncio"]
