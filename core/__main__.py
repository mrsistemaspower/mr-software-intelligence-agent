"""Linha de comando do núcleo.

    py -m core validate-pack <caminho> [<caminho> ...]
    py -m core validate-packs
    py -m core check-isolation
    py -m core check-schemas

Código de saída 0 = sem problemas; 1 = problemas encontrados; 2 = uso incorreto.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import isolation
from .pack import discover_packs, validate_pack
from .schema_lite import Registry

REPO = Path(__file__).resolve().parent.parent


def _report(label: str, problems: list) -> int:
    if problems:
        print(f"FALHA {label}: {len(problems)} problema(s)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"OK    {label}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    status = 0
    if command == "validate-pack" and rest:
        registry = Registry()
        for path in rest:
            status |= _report(path, validate_pack(path, registry))
        return status
    if command == "validate-packs":
        registry = Registry()
        packs = discover_packs(REPO / "software-packs", REPO / "evaluations" / "fixtures")
        for path in packs:
            status |= _report(path.relative_to(REPO).as_posix(), validate_pack(path, registry))
        return status
    if command == "check-isolation":
        return _report("isolamento", isolation.check_all(REPO))
    if command == "check-schemas":
        return _report("schemas", Registry().check_all())
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
