"""Proteção de raízes de fonte: o agente distingue o que lê, o que está congelado e onde pode escrever.

Cada raiz é declarada por NOME e recebe uma classe de proteção. O caminho local da raiz é detalhe de execução e
nunca entra num contrato portátil.

    READ_ONLY       o agente nunca escreve aqui. O conteúdo pode mudar ENTRE execuções (a pessoa edita a fonte): isso
                    aparece como mudança de entrada e invalida o que depende dela. Mudar DURANTE a execução, sem que o
                    agente tenha pedido, é violação.
    FROZEN          evidência fixada. A linha de base é gravada na primeira execução e vale para sempre: qualquer
                    diferença, em qualquer momento, é violação. Uma raiz FROZEN não pode ter verificação desligada.
    MUTABLE_OUTPUT  única classe em que o agente escreve (estado, checkpoints e saídas dos estágios).

Verificação (`Verify`): `EACH_STAGE` (depois de cada estágio e no fim), `RUN_END` (só no fim) ou `NONE` (só para
READ_ONLY). A linha de base é um `KnowledgeSnapshot` (reuso de `core.snapshot`) e a conferência é `core.integrity`:
ausente, alterado, surgido, ilegível ou fora da lista.

O que isto garante e o que NÃO garante:

- A API de escrita do agente só aceita raízes MUTABLE_OUTPUT. Isto não é sandbox: um tratador de estágio que escreva
  por conta própria numa raiz protegida não é impedido, é DETECTADO na próxima verificação.
- Violação marca a execução como insegura. O agente NÃO reverte, apaga nem corrige nada: o que a pessoa ou outro
  processo alterou pode ser dado dela e não é do agente decidir.
- Ligações simbólicas de diretório dentro de uma raiz protegida não são seguidas. Arquivo cujo nome não é um caminho
  lógico portátil impede a captura (`ProtectionError`), porque não poderia ser descrito sem perda.
- A conferência e a leitura seguinte são dois momentos distintos.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from .integrity import IntegrityReport
from .paths import PathPortabilityError, normalize_logical_path, resolve_logical, to_fs_path
from .snapshot import KnowledgeSnapshot, SnapshotError, load_snapshot, save_snapshot, snapshot_directory, verify_snapshot
from .stage_graph import is_valid_name

PROTECTED_ROLE = "protected"
POINT_STAGE = "STAGE"
POINT_END = "END"


class ProtectionError(Exception):
    """Configuração de proteção inválida, ou raiz impossível de capturar."""


class Protection(str, Enum):
    READ_ONLY = "READ_ONLY"
    FROZEN = "FROZEN"
    MUTABLE_OUTPUT = "MUTABLE_OUTPUT"


class Verify(str, Enum):
    EACH_STAGE = "EACH_STAGE"
    RUN_END = "RUN_END"
    NONE = "NONE"


@dataclass(frozen=True)
class RootPolicy:
    """Política de uma raiz: nome, classe de proteção, quando verificar e o escopo (caminhos lógicos; `.` = tudo)."""

    name: str
    protection: Protection
    verify: Verify | None = None
    scope: tuple[str, ...] = (".",)

    def __post_init__(self) -> None:
        if not is_valid_name(self.name):
            raise ProtectionError(f"nome de raiz inválido: {self.name!r}")
        try:
            protection = Protection(self.protection)
        except ValueError:
            raise ProtectionError(f"{self.name}: classe de proteção desconhecida: {self.protection!r}") from None
        object.__setattr__(self, "protection", protection)
        verify = self.verify
        if verify is None:
            verify = Verify.NONE if protection is Protection.MUTABLE_OUTPUT else Verify.EACH_STAGE
        try:
            verify = Verify(verify)
        except ValueError:
            raise ProtectionError(f"{self.name}: modo de verificação desconhecido: {self.verify!r}") from None
        if protection is Protection.FROZEN and verify is Verify.NONE:
            raise ProtectionError(f"{self.name}: uma raiz FROZEN não pode ter a verificação desligada")
        if protection is Protection.MUTABLE_OUTPUT and verify is not Verify.NONE:
            raise ProtectionError(f"{self.name}: raiz MUTABLE_OUTPUT não tem integridade fixa para verificar")
        object.__setattr__(self, "verify", verify)
        scope = self.scope
        if isinstance(scope, str) or not isinstance(scope, Iterable):
            raise ProtectionError(f"{self.name}: scope deve ser uma lista de caminhos lógicos")
        normalized: set[str] = set()
        for entry in scope:
            if entry == ".":
                normalized.add(".")
                continue
            try:
                normalized.add(normalize_logical_path(entry))
            except PathPortabilityError as exc:
                raise ProtectionError(f"{self.name}: escopo inválido ({exc})") from exc
        if not normalized:
            raise ProtectionError(f"{self.name}: o escopo não pode ser vazio")
        if protection is Protection.MUTABLE_OUTPUT and normalized != {"."}:
            raise ProtectionError(f"{self.name}: raiz MUTABLE_OUTPUT não tem escopo")
        object.__setattr__(self, "scope", tuple(sorted(normalized)))

    @property
    def protected(self) -> bool:
        return self.protection is not Protection.MUTABLE_OUTPUT

    def to_dict(self) -> dict:
        return {"name": self.name, "protection": self.protection.value, "verify": self.verify.value, "scope": list(self.scope)}

    @classmethod
    def from_dict(cls, data: object) -> "RootPolicy":
        if not isinstance(data, dict) or not {"name", "protection"} <= set(data) or set(data) - {"name", "protection", "verify", "scope"}:
            raise ProtectionError("política de raiz malformada")
        return cls(data["name"], data["protection"], data.get("verify"), tuple(data.get("scope", (".",))))


@dataclass(frozen=True)
class Violation:
    """Mudança inesperada numa raiz protegida. Só nome da raiz, tipo e caminho lógico: nunca conteúdo nem caminho local."""

    root: str
    protection: str
    kind: str
    path: str

    def __str__(self) -> str:
        return f"{self.kind} {self.root}:{self.path}"

    def to_dict(self) -> dict:
        return {"root": self.root, "protection": self.protection, "kind": self.kind, "path": self.path}

    @classmethod
    def from_dict(cls, data: object) -> "Violation":
        if not isinstance(data, dict) or set(data) != {"root", "protection", "kind", "path"}:
            raise ProtectionError("violação malformada")
        return cls(data["root"], data["protection"], data["kind"], data["path"])


@dataclass(frozen=True)
class ProtectionRecord:
    """O que a execução registra de cada raiz: classe, verificação e identidade da linha de base."""

    root: str
    protection: str
    verify: str
    baseline_snapshot_id: str | None = None
    entries: int = 0

    def to_dict(self) -> dict:
        data: dict = {"root": self.root, "protection": self.protection, "verify": self.verify, "entries": self.entries}
        if self.baseline_snapshot_id is not None:
            data["baseline_snapshot_id"] = self.baseline_snapshot_id
        return data

    @classmethod
    def from_dict(cls, data: object) -> "ProtectionRecord":
        if not isinstance(data, dict) or not {"root", "protection", "verify", "entries"} <= set(data) or set(data) - {
            "root", "protection", "verify", "entries", "baseline_snapshot_id"
        }:
            raise ProtectionError("registro de proteção malformado")
        return cls(data["root"], data["protection"], data["verify"], data.get("baseline_snapshot_id"), data["entries"])


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _walk_files(root: Path, prefix: str) -> list[str]:
    base = resolve_logical(root, prefix) if prefix else root
    start = to_fs_path(base, tree=True)
    found: list[str] = []
    for current, directories, files in os.walk(start):
        directories.sort()
        relative_dir = os.path.relpath(current, start)
        # O nome do arquivo entra verbatim: `relpath` normaliza (no Windows remove ponto final), e a checagem de
        # portabilidade precisa ver o nome como ele é.
        parts = [] if relative_dir == "." else relative_dir.split(os.sep)
        for name in sorted(files):
            logical = "/".join(([prefix] if prefix else []) + parts + [name])
            try:
                found.append(normalize_logical_path(logical))
            except PathPortabilityError as exc:
                raise ProtectionError("a raiz protegida contém arquivo com nome não portátil; não pode ser descrita sem perda") from exc
    return found


class ProtectionGuard:
    """Captura e confere a integridade das raízes protegidas de uma execução, e concede caminhos de escrita."""

    def __init__(
        self,
        policies: Iterable[RootPolicy],
        bindings: Mapping[str, "str | os.PathLike[str]"],
        *,
        baseline_dir: "str | os.PathLike[str]",
        software_id: str | None = None,
    ) -> None:
        items = list(policies)
        if not all(isinstance(item, RootPolicy) for item in items):
            raise TypeError("policies deve conter RootPolicy")
        names = [item.name for item in items]
        if len(set(names)) != len(names):
            raise ProtectionError("nome de raiz repetido")
        if set(bindings) != set(names):
            missing = sorted(set(names) - set(bindings))
            extra = sorted(set(bindings) - set(names))
            raise ProtectionError(f"vínculos locais não conferem com as raízes declaradas (faltam: {missing}; sobram: {extra})")
        self._policies = {item.name: item for item in sorted(items, key=lambda p: p.name)}
        self._paths = {name: Path(bindings[name]).resolve() for name in self._policies}
        ordered = sorted(self._paths)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                a, b = self._paths[first], self._paths[second]
                if _inside(a, b) or _inside(b, a):
                    raise ProtectionError(f"as raízes {first} e {second} se sobrepõem no disco; cada raiz precisa de uma árvore própria")
        for name, policy in self._policies.items():
            if policy.protected and not self._paths[name].is_dir():
                raise ProtectionError(f"a raiz protegida {name} não existe como diretório")
        self._baseline_dir = Path(baseline_dir)
        self._software_id = software_id
        self._baselines: dict[str, KnowledgeSnapshot] = {}

    # ------------------------------------------------------------------ consulta
    def policy(self, root: str) -> RootPolicy:
        try:
            return self._policies[root]
        except KeyError:
            raise ProtectionError(f"raiz desconhecida: {root}") from None

    def local_root(self, root: str) -> Path:
        self.policy(root)
        return self._paths[root]

    def output_path(self, root: str, logical: str) -> Path:
        """Caminho local de escrita. Só raízes MUTABLE_OUTPUT concedem escrita."""
        policy = self.policy(root)
        if policy.protection is not Protection.MUTABLE_OUTPUT:
            raise ProtectionError(f"escrita recusada: a raiz {root} é {policy.protection.value}")
        return resolve_logical(self._paths[root], logical)

    def read_path(self, root: str, logical: str) -> Path:
        self.policy(root)
        return resolve_logical(self._paths[root], logical)

    # ------------------------------------------------------------------ captura e conferência
    def _capture(self, policy: RootPolicy) -> KnowledgeSnapshot:
        root = self._paths[policy.name]
        chosen: dict[str, None] = {}
        try:
            for entry in policy.scope:
                if entry == ".":
                    files = _walk_files(root, "")
                else:
                    target = resolve_logical(root, entry)
                    files = _walk_files(root, entry) if target.is_dir() else [entry]
                for item in files:
                    chosen.setdefault(item, None)
            specs = [(path, PROTECTED_ROLE) for path in sorted(chosen)]
            return snapshot_directory(root, specs, label=f"raiz protegida {policy.name}", software_id=self._software_id)
        except (SnapshotError, PathPortabilityError, OSError) as exc:
            raise ProtectionError(f"não foi possível capturar a raiz {policy.name}: {type(exc).__name__}") from exc

    def _baseline_file(self, name: str) -> Path:
        return self._baseline_dir / f"{name}.snapshot.json"

    def _undeclared_scopes(self, policy: RootPolicy) -> list[str]:
        root = self._paths[policy.name]
        scopes = []
        for entry in policy.scope:
            if entry == "." or resolve_logical(root, entry).is_dir():
                scopes.append(entry)
        return scopes

    def _violations(self, policy: RootPolicy) -> tuple[Violation, ...]:
        baseline = self._baselines[policy.name]
        report: IntegrityReport = verify_snapshot(
            baseline, self._paths[policy.name], undeclared_within=self._undeclared_scopes(policy)
        )
        return tuple(Violation(policy.name, policy.protection.value, item.kind, item.path) for item in report.findings)

    def begin(self) -> tuple[tuple[ProtectionRecord, ...], tuple[Violation, ...]]:
        """Abre a execução: READ_ONLY ganha linha de base nova; FROZEN usa a gravada (ou grava na primeira vez).

        Devolve os registros de proteção e as violações já presentes (só FROZEN pode ter).
        """
        violations: list[Violation] = []
        for name, policy in self._policies.items():
            if not policy.protected:
                continue
            if policy.protection is Protection.READ_ONLY:
                self._baselines[name] = self._capture(policy)
                continue
            stored = self._baseline_file(name)
            if os.path.exists(to_fs_path(stored)):
                try:
                    baseline = load_snapshot(stored)
                except (SnapshotError, OSError, ValueError) as exc:
                    raise ProtectionError(f"a linha de base da raiz FROZEN {name} está ilegível ou adulterada: {type(exc).__name__}") from exc
                if baseline.software_id != self._software_id:
                    raise ProtectionError(f"a linha de base da raiz FROZEN {name} pertence a outro software")
                self._baselines[name] = baseline
                violations.extend(self._violations(policy))
            else:
                baseline = self._capture(policy)
                save_snapshot(stored, baseline)
                self._baselines[name] = baseline
        records = tuple(
            ProtectionRecord(
                root=name,
                protection=policy.protection.value,
                verify=policy.verify.value,
                baseline_snapshot_id=self._baselines[name].snapshot_id if name in self._baselines else None,
                entries=len(self._baselines[name].entries) if name in self._baselines else 0,
            )
            for name, policy in self._policies.items()
        )
        return records, tuple(violations)

    def verify(self, point: str) -> tuple[Violation, ...]:
        """Confere as raízes que a política manda conferir neste ponto (`STAGE` ou `END`). Não modifica nada."""
        if point not in (POINT_STAGE, POINT_END):
            raise ValueError("point deve ser STAGE ou END")
        found: list[Violation] = []
        for name, policy in self._policies.items():
            if not policy.protected:
                continue
            due = policy.verify is Verify.EACH_STAGE or (point == POINT_END and policy.verify is Verify.RUN_END)
            if not due:
                continue
            if name not in self._baselines:
                raise ProtectionError("begin() deve ser chamado antes de verify()")
            found.extend(self._violations(policy))
        return tuple(found)
