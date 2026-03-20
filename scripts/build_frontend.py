from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
STATIC_DIR = ROOT_DIR / "mycode" / "server" / "static"


def build_frontend() -> None:
    if not FRONTEND_DIR.is_dir():
        raise RuntimeError(f"frontend source directory not found: {FRONTEND_DIR}")

    _run_pnpm(["install", "--frozen-lockfile"], cwd=FRONTEND_DIR)
    _run_pnpm(["build"], cwd=FRONTEND_DIR)

    if not FRONTEND_DIST_DIR.is_dir():
        raise RuntimeError(f"frontend build output not found: {FRONTEND_DIST_DIR}")

    if STATIC_DIR.exists():
        shutil.rmtree(STATIC_DIR)
    shutil.copytree(FRONTEND_DIST_DIR, STATIC_DIR)


def _run_pnpm(args: list[str], *, cwd: Path) -> None:
    try:
        subprocess.run(["pnpm", *args], cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("pnpm is required to build the frontend assets") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pnpm {' '.join(args)} failed with exit code {exc.returncode}") from exc


if __name__ == "__main__":
    build_frontend()
