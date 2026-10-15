"""Utilitários compartilhados pelos testes. Só biblioteca padrão."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURE = REPO / "evaluations" / "fixtures" / "acme-ledger"
SYSCOM = REPO / "software-packs" / "syscom"
TEMPLATE = REPO / "software-packs" / "_template"

COMMON = "urn:mr-software-intelligence:schema:common:1"


def load_json(path: Path | str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path | str, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_repo(dst: Path) -> Path:
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    return dst


def copy_pack(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def link_directory(link: Path, target: Path) -> None:
    """Cria uma ligação de diretório: symlink ou, no Windows sem privilégio, junção. Pula o teste se não der."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        # Saída lida como bytes: o console do Windows não usa UTF-8 e a mensagem não interessa.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if result.returncode == 0 and link.exists():
            return
    raise unittest.SkipTest("ligação de diretório indisponível neste ambiente")


class TempDirCase(unittest.TestCase):
    """Caso de teste com um diretório temporário por teste."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
