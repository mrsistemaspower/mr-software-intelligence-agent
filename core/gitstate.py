"""Adaptador OPCIONAL de estado do Git: uma capacidade do ambiente, nunca um requisito do núcleo.

O Agent Core não exige Git para nenhum software. Nenhum outro módulo importa este; o orquestrador só aceita, por
injeção, qualquer objeto com os métodos `capture()` e `compare()` (ver `GitStateProbe`). Sem sonda, a execução não
sabe que Git existe.

O que captura (somente leitura, comandos fixos, sem shell, sem rede):

- `available`: há um repositório utilizável? Git ausente, diretório fora de repositório, tempo esgotado ou erro
  viram `available=False` com um motivo estável (`NOT_INSTALLED`, `NOT_A_REPOSITORY`, `TIMEOUT`, `ERROR`). Nunca levanta.
- `head`: commit atual (`None` num repositório sem commits) e `branch` (`None` com HEAD destacado).
- `dirty`: há alteração em arquivo rastreado (preparada ou não). Arquivos não rastreados NÃO sujam; são contados à parte.
- `tracked_changes`: caminhos lógicos (relativos à raiz do repositório) com alteração rastreada, ordenados.
  Caminho que não é portátil entra só na contagem `unportable_changes`.

Usa `--no-optional-locks` para que consultar não grave nada no repositório. Fora de escopo, por decisão: qualquer chamada
a API de hospedagem, `fetch`, `push`, `remote`, escrita, `checkout` ou `reset`. A captura descreve; quem decide é a pessoa.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable

from .paths import PathPortabilityError, normalize_logical_path

NOT_INSTALLED = "NOT_INSTALLED"
NOT_A_REPOSITORY = "NOT_A_REPOSITORY"
TIMEOUT = "TIMEOUT"
ERROR = "ERROR"
REASONS = (NOT_INSTALLED, NOT_A_REPOSITORY, TIMEOUT, ERROR)

DEFAULT_TIMEOUT = 30
_HEAD = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_BRANCH_LIMIT = 200

Runner = Callable[[list, str], "tuple[int, bytes]"]


class GitUnavailable(Exception):
    """Interno: o executor não conseguiu rodar o comando. `reason` é um dos motivos estáveis."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def subprocess_runner(timeout: int = DEFAULT_TIMEOUT) -> Runner:
    """Executor padrão: lista de argumentos (sem shell), sem entrada, com limite de tempo."""

    def run(args: list, cwd: str) -> "tuple[int, bytes]":
        try:
            result = subprocess.run(
                ["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, shell=False, check=False,
            )
        except FileNotFoundError:
            raise GitUnavailable(NOT_INSTALLED) from None
        except subprocess.TimeoutExpired:
            raise GitUnavailable(TIMEOUT) from None
        except OSError:
            raise GitUnavailable(ERROR) from None
        return result.returncode, result.stdout

    return run


@dataclass(frozen=True)
class GitState:
    available: bool
    reason: str | None = None
    head: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    tracked_changes: tuple[str, ...] = ()
    untracked_count: int | None = None
    unportable_changes: int = 0

    def __post_init__(self) -> None:
        if self.available:
            if self.reason is not None:
                raise ValueError("estado disponível não tem motivo de indisponibilidade")
            if self.dirty is None or self.untracked_count is None:
                raise ValueError("estado disponível exige dirty e untracked_count")
        else:
            if self.reason not in REASONS:
                raise ValueError(f"motivo de indisponibilidade inválido: {self.reason!r}")
            if any(v is not None for v in (self.head, self.branch, self.dirty, self.untracked_count)) or self.tracked_changes:
                raise ValueError("estado indisponível não carrega dados do repositório")
        if self.head is not None and not _HEAD.match(self.head):
            raise ValueError("head inválido")
        object.__setattr__(self, "tracked_changes", tuple(self.tracked_changes))

    def to_dict(self) -> dict:
        if not self.available:
            return {"available": False, "reason": self.reason}
        data: dict = {"available": True}
        if self.head is not None:
            data["head"] = self.head
        if self.branch is not None:
            data["branch"] = self.branch
        data.update(
            dirty=self.dirty,
            tracked_changes=list(self.tracked_changes),
            untracked_count=self.untracked_count,
            unportable_changes=self.unportable_changes,
        )
        return data

    @classmethod
    def from_dict(cls, data: object) -> "GitState":
        if not isinstance(data, dict) or "available" not in data:
            raise ValueError("estado de git malformado")
        if not data["available"]:
            return cls(False, data.get("reason"))
        return cls(
            True, None, data.get("head"), data.get("branch"), data["dirty"], tuple(data["tracked_changes"]),
            data["untracked_count"], data.get("unportable_changes", 0),
        )


def _parse_status(raw: bytes) -> tuple[list[str], int, int]:
    """Saída de `status --porcelain=v1 -z`: (caminhos rastreados alterados, não rastreados, não portáteis)."""
    tokens = raw.split(b"\0")
    tracked: list[str] = []
    untracked = 0
    unportable = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        status, name = token[:2].decode("ascii", "replace"), token[3:].decode("utf-8", "replace")
        if status == "??":
            untracked += 1
            continue
        if status == "!!":
            continue
        if "R" in status or "C" in status:  # renomeação ou cópia: o próximo token é o caminho de origem
            index += 1
        try:
            tracked.append(normalize_logical_path(name))
        except PathPortabilityError:
            unportable += 1
    return sorted(set(tracked)), untracked, unportable


def capture_git_state(
    local_root: "str | os.PathLike[str]", *, scope: Iterable[str] = (), runner: Runner | None = None, timeout: int = DEFAULT_TIMEOUT
) -> GitState:
    """Estado do repositório que contém `local_root`. Nunca levanta: ausência de Git é um resultado, não um erro.

    `scope`: caminhos lógicos (relativos à raiz do repositório) que limitam `tracked_changes` e `untracked_count`.
    """
    run = runner if runner is not None else subprocess_runner(timeout)
    cwd = os.fspath(local_root)
    base = ["--no-optional-locks"]
    try:
        code, out = run(base + ["rev-parse", "--is-inside-work-tree"], cwd)
        if code != 0 or out.strip() != b"true":
            return GitState(False, NOT_A_REPOSITORY)
        code, out = run(base + ["rev-parse", "--verify", "-q", "HEAD"], cwd)
        head = out.strip().decode("ascii", "replace") if code == 0 else None
        if head is not None and not _HEAD.match(head):
            head = None
        code, out = run(base + ["symbolic-ref", "--short", "-q", "HEAD"], cwd)
        branch = out.strip().decode("utf-8", "replace")[:_BRANCH_LIMIT] if code == 0 and out.strip() else None
        pathspec: list[str] = []
        for entry in scope:
            pathspec.append(normalize_logical_path(entry))
        command = base + ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]
        if pathspec:
            command += ["--", *pathspec]
        code, out = run(command, cwd)
        if code != 0:
            return GitState(False, ERROR)
    except GitUnavailable as exc:
        return GitState(False, exc.reason)
    except (OSError, ValueError, PathPortabilityError):
        return GitState(False, ERROR)
    tracked, untracked, unportable = _parse_status(out)
    return GitState(True, None, head, branch, bool(tracked or unportable), tuple(tracked), untracked, unportable)


def git_drift(before: GitState, after: GitState) -> tuple[str, ...]:
    """O que mudou entre duas capturas, em códigos estáveis e ordenados. Informação, não veredito."""
    if before.available != after.available:
        return ("AVAILABILITY_CHANGED",)
    if not before.available:
        return ()
    changes = []
    if before.head != after.head:
        changes.append("HEAD_CHANGED")
    if before.branch != after.branch:
        changes.append("BRANCH_CHANGED")
    if before.dirty != after.dirty:
        changes.append("DIRTY_CHANGED")
    if before.tracked_changes != after.tracked_changes:
        changes.append("TRACKED_CHANGES_CHANGED")
    return tuple(changes)


class GitStateProbe:
    """Sonda injetável no orquestrador. `capture()` devolve um dicionário portátil; `compare()`, os códigos de deriva."""

    def __init__(
        self, local_root: "str | os.PathLike[str]", *, scope: Iterable[str] = (), runner: Runner | None = None
    ) -> None:
        self._root = local_root
        self._scope = tuple(scope)
        self._runner = runner

    def capture(self) -> dict:
        return capture_git_state(self._root, scope=self._scope, runner=self._runner).to_dict()

    def compare(self, before: dict, after: dict) -> list:
        return list(git_drift(GitState.from_dict(before), GitState.from_dict(after)))
