import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def repo_cwd():
    """Тесты используют пути от корня репо (корпуса текстов, конфиги)."""
    os.chdir(REPO_ROOT)


@pytest.fixture(scope="session")
def ref_audio(repo_cwd) -> Path:
    """Синтетические референс-wav для режима clone (генерируются при отсутствии)."""
    out = REPO_ROOT / "assets" / "ref_audio"
    if not list(out.glob("*.wav")):
        subprocess.run(
            [sys.executable, "scripts/gen_ref_audio.py", "--out", str(out)],
            check=True, cwd=REPO_ROOT,
        )
    return out
