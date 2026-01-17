import logging


def setup_logging() -> None:
    """Configure default logging."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
