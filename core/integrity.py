"""Integridade de fontes: "eu processei exatamente esta fonte".

Compara o que se esperava (caminho lógico + hash) com o que existe agora numa raiz local, e relata:

- `MISSING`: esperado presente, ausente no disco;
- `MUTATED`: presente, mas com hash (ou tamanho) diferente do esperado;
- `APPEARED`: esperado ausente, e o arquivo existe (uma camada de conhecimento surgiu);
- `UNREADABLE`: não deu para ler (diretório no lugar de arquivo, permissão, caminho que escapa da raiz);
- `UNDECLARED`: arquivo presente sob um escopo pedido, mas fora da lista esperada (opcional).

Duplicidade de conteúdo (arquivos diferentes, mesmo hash) é só informativa e vai em `duplicates`.

Para operações que exigem evidência congelada, use `require_intact` ou `IntegrityReport.raise_if_failed`:
a execução não segue adiante em silêncio depois de um achado. Nenhum relatório carrega caminho absoluto
nem o texto de erro do sistema operacional (que costuma incluí-lo).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .hashing import Digest, hash_file
from .paths import (
    PathPortabilityError,
    comparison_key,
    is_logical_path,
    normalize_logical_path,
    resolve_logical,
    to_fs_path,
)

MISSING = "MISSING"
MUTATED = "MUTATED"
APPEARED = "APPEARED"
UNREADABLE = "UNREADABLE"
UNDECLARED = "UNDECLARED"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_LIMIT = 10


@dataclass(frozen=True)
class ExpectedSource:
    """O que se espera de uma fonte. `sha256=None` significa "deve estar ausente"."""

    path: str
    sha256: str | None = None
    byte_length: int | None = None

    def __post_init__(self) -> None:
        if not is_logical_path(self.path):
            raise PathPortabilityError("ExpectedSource.path deve ser um caminho lógico normalizado")
        if self.sha256 is not None and not (isinstance(self.sha256, str) and _SHA256.match(self.sha256)):
            raise ValueError("sha256 deve ter 64 hexadecimais minúsculos")
        if self.byte_length is not None:
            if self.sha256 is None:
                raise ValueError("fonte esperada ausente não tem tamanho")
            if type(self.byte_length) is not int or self.byte_length < 0:
                raise ValueError("byte_length deve ser inteiro não negativo")


def expect_present(path: str, digest: "Digest | str", byte_length: int | None = None) -> ExpectedSource:
    """Expectativa de fonte presente. Aceita um `Digest` (que já traz o tamanho) ou 64 hexadecimais."""
    if isinstance(digest, Digest):
        return ExpectedSource(normalize_logical_path(path), digest.digest, digest.byte_length)
    return ExpectedSource(normalize_logical_path(path), digest, byte_length)


def expect_absent(path: str) -> ExpectedSource:
    return ExpectedSource(normalize_logical_path(path), None, None)


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    expected: str | None = None
    actual: str | None = None
    detail: str = ""

    def __str__(self) -> str:
        extra = f" ({self.detail})" if self.detail else ""
        return f"{self.kind} {self.path}{extra}"


@dataclass(frozen=True)
class IntegrityReport:
    checked: int
    findings: tuple[Finding, ...]
    duplicates: tuple[tuple[str, ...], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        if self.ok:
            return f"integridade confirmada em {self.checked} fonte(s)"
        shown = "; ".join(str(f) for f in self.findings[:_SUMMARY_LIMIT])
        more = f"; e mais {len(self.findings) - _SUMMARY_LIMIT}" if len(self.findings) > _SUMMARY_LIMIT else ""
        return f"integridade violada: {len(self.findings)} achado(s): {shown}{more}"

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise IntegrityError(self)


class IntegrityError(Exception):
    """A evidência congelada não confere. Carrega o relatório completo em `report`."""

    def __init__(self, report: IntegrityReport) -> None:
        super().__init__(report.summary())
        self.report = report


def _prepare(expected: Iterable[ExpectedSource]) -> list[ExpectedSource]:
    by_path: dict[str, ExpectedSource] = {}
    for item in expected:
        if not isinstance(item, ExpectedSource):
            raise TypeError("expected deve conter ExpectedSource")
        previous = by_path.get(item.path)
        if previous is not None and previous != item:
            raise ValueError(f"expectativas conflitantes para o mesmo caminho: {item.path}")
        by_path[item.path] = item
    return [by_path[path] for path in sorted(by_path)]


def _describe(error: OSError) -> str:
    """Descrição sem o caminho: o texto padrão de OSError inclui o nome do arquivo."""
    code = getattr(error, "errno", None)
    reason = os.strerror(code) if isinstance(code, int) and code else ""
    return type(error).__name__ + (f": {reason}" if reason else "")


def _check_one(root: Path, item: ExpectedSource, hashed: dict[str, list[str]]) -> list[Finding]:
    wanted = f"sha256:{item.sha256}" if item.sha256 else None
    try:
        target = resolve_logical(root, item.path)
    except PathPortabilityError as exc:
        return [Finding(UNREADABLE, item.path, wanted, None, str(exc))]
    location = to_fs_path(target)
    if not os.path.exists(location):
        if item.sha256 is None:
            return []
        return [Finding(MISSING, item.path, wanted, None, "arquivo ausente")]
    if item.sha256 is None:
        return [Finding(APPEARED, item.path, None, None, "existe onde se esperava ausência")]
    if not os.path.isfile(location):
        return [Finding(UNREADABLE, item.path, wanted, None, "não é um arquivo regular")]
    try:
        digest = hash_file(target)
    except OSError as exc:
        return [Finding(UNREADABLE, item.path, wanted, None, _describe(exc))]
    hashed.setdefault(digest.digest, []).append(item.path)
    wrong_hash = digest.digest != item.sha256
    wrong_size = item.byte_length is not None and digest.byte_length != item.byte_length
    if not (wrong_hash or wrong_size):
        return []
    detail = "conteúdo diferente do esperado"
    if item.byte_length is not None:
        detail += f"; bytes esperados {item.byte_length}, lidos {digest.byte_length}"
    return [Finding(MUTATED, item.path, wanted, str(digest), detail)]


def _undeclared(root: Path, scopes: Iterable[str], declared: set[str]) -> list[Finding]:
    known = {comparison_key(path) for path in declared}
    base = Path(root).resolve()
    seen: set[str] = set()
    for scope in scopes:
        prefix = "" if scope in ("", ".") else normalize_logical_path(scope)
        start = base if not prefix else resolve_logical(base, prefix)
        start_fs = to_fs_path(start, tree=True)
        for current, directories, files in os.walk(start_fs):
            directories.sort()
            for name in sorted(files):
                relative = os.path.relpath(os.path.join(current, name), start_fs).replace(os.sep, "/")
                seen.add(f"{prefix}/{relative}" if prefix else relative)
    return [
        Finding(UNDECLARED, path, None, None, "arquivo fora da lista esperada")
        for path in sorted(seen)
        if comparison_key(path) not in known
    ]


def verify_sources(
    local_root: str | os.PathLike[str],
    expected: Iterable[ExpectedSource],
    *,
    detect_duplicates: bool = False,
    undeclared_within: Iterable[str] = (),
) -> IntegrityReport:
    """Confere `expected` contra os arquivos sob `local_root`. Não modifica nada.

    `undeclared_within`: caminhos lógicos de diretórios (use `"."` para a raiz inteira) cujos arquivos
    devem todos constar em `expected`; o que sobrar vira `UNDECLARED`.
    """
    items = _prepare(expected)
    root = Path(local_root)
    hashed: dict[str, list[str]] = {}
    findings: list[Finding] = []
    for item in items:
        findings.extend(_check_one(root, item, hashed))
    scopes = list(undeclared_within)
    if scopes:
        findings.extend(_undeclared(root, scopes, {item.path for item in items}))
    duplicates: tuple[tuple[str, ...], ...] = ()
    if detect_duplicates:
        duplicates = tuple(sorted(tuple(sorted(paths)) for paths in hashed.values() if len(paths) > 1))
    ordered = tuple(sorted(findings, key=lambda f: (f.path, f.kind)))
    return IntegrityReport(checked=len(items), findings=ordered, duplicates=duplicates)


def require_intact(
    local_root: str | os.PathLike[str],
    expected: Iterable[ExpectedSource],
    *,
    undeclared_within: Iterable[str] = (),
) -> IntegrityReport:
    """Como `verify_sources`, mas levanta `IntegrityError` ao primeiro relatório com achados."""
    report = verify_sources(local_root, expected, undeclared_within=undeclared_within)
    report.raise_if_failed()
    return report


def expected_from_source_records(
    records: Iterable[dict], *, workspace: str | None = None, require_pinned: bool = True
) -> list[ExpectedSource]:
    """Expectativas a partir de registros `source` do pacote (`locator.path` + `sha256`).

    Registros de mais de um workspace exigem `workspace`, porque uma verificação usa uma só raiz local.
    Fonte sem hash fixado não pode ser afirmada: com `require_pinned=True` (padrão) isso é um erro.
    """
    selected = [record for record in records if isinstance(record, dict)]
    workspaces = {record["locator"]["workspace"] for record in selected}
    if workspace is None and len(workspaces) > 1:
        raise ValueError("registros de mais de um workspace: informe qual verificar")
    if workspace is not None:
        selected = [record for record in selected if record["locator"]["workspace"] == workspace]
    expected: list[ExpectedSource] = []
    unpinned: list[str] = []
    for record in selected:
        sha = record.get("sha256")
        if sha is None:
            unpinned.append(str(record.get("id", "?")))
            continue
        expected.append(expect_present(record["locator"]["path"], sha))
    if unpinned and require_pinned:
        raise ValueError("fontes sem hash fixado não podem ser verificadas: " + ", ".join(sorted(unpinned)))
    return expected
