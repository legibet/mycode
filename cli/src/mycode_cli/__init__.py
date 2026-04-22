"""mycode-cli — interactive CLI and FastAPI server for the mycode agent."""

from importlib import metadata

# The package metadata in cli/pyproject.toml is the single version source.
__version__ = metadata.version("mycode-cli")
