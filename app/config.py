"""Application configuration and logging setup."""

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    default_model: str | None
    api_base: str | None
    port: int


def get_settings() -> Settings:
    """Load runtime settings from environment."""
    return Settings(
        default_model=os.environ.get("MODEL"),
        api_base=os.environ.get("BASE_URL"),
        port=int(os.environ.get("PORT", "8000")),
    )


def setup_logging() -> None:
    """Configure default logging."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
