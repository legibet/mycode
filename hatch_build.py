from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def build_frontend_assets() -> None:
    from scripts.build_frontend import build_frontend

    build_frontend()


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        del version, build_data
        build_frontend_assets()


if __name__ == "__main__":
    build_frontend_assets()
