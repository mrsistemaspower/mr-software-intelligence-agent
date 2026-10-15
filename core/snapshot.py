"""Snapshot de conhecimento: descrição determinística do estado de conhecimento que uma operação consumiu.

Um snapshot NÃO copia fontes. Lista artefatos por caminho lógico, papel, situação e hash. Serve para
afirmar "esta operação leu este estado" e para detectar, depois, que o estado mudou.

Identidade (`snapshot_id`) deriva só do conteúdo:

    snapshot_id = hash( identity_version, software_id (se houver), hash das entradas ordenadas )

Fora da identidade, de propósito: `created_from`, `parent_snapshot_id` e tudo em `generation`
(gerador, `created_at`, notas). Um relógio, uma máquina ou um provedor de modelo diferentes não mudam o
estado de conhecimento e, portanto, não mudam o id. Nenhuma função deste módulo lê o relógio:
`created_at` é sempre fornecido por quem chama.

Regras de conteúdo:

- entradas ordenadas por caminho lógico, uma por caminho (e nenhuma colisão sem diferença de caixa);
- situação `PRESENT` (com hash e tamanho) ou `ABSENT` (sem hash). Uma camada de conhecimento que
  deveria existir e ainda não existe é registrada como `ABSENT`, e o dia em que ela aparecer muda o id;
- nenhum caminho local: só caminhos lógicos (ver `core.paths`).

O contrato JSON está em `schemas/knowledge-snapshot.schema.json`.
"""
from __future__ import annotations

import datetime as _dt
import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, NamedTuple

from . import __version__
from .atomic import WriteResult, atomic_write_json
from .hashing import Digest, hash_bytes, hash_canonical_json, hash_file
from .integrity import IntegrityReport, expect_absent, expect_present, verify_sources
from .paths import find_local_paths, is_logical_path, normalize_logical_path, resolve_logical, to_fs_path
from .schema_lite import Registry, Validator
from .serialization import loads_strict

SCHEMA_ID = "urn:mr-software-intelligence:schema:knowledge-snapshot:1"
SCHEMA_NAME = "knowledge-snapshot"
IDENTITY_VERSION = 1
PRESENT = "PRESENT"
ABSENT = "ABSENT"
CREATED_FROM_KINDS = ("directory", "pack", "declared-list", "derived")
DEFAULT_GENERATOR = f"core.snapshot/{__version__}"

ROLE_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
SOFTWARE_ID_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
SNAPSHOT_ID_PATTERN = r"^snap:sha256:[0-9a-f]{64}$"

_ROLE = re.compile(ROLE_PATTERN)
_SOFTWARE_ID = re.compile(SOFTWARE_ID_PATTERN)
_SNAPSHOT_ID = re.compile(SNAPSHOT_ID_PATTERN)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

ADDED = "ADDED"
REMOVED = "REMOVED"
MODIFIED = "MODIFIED"
APPEARED = "APPEARED"
DISAPPEARED = "DISAPPEARED"
ROLE_CHANGED = "ROLE_CHANGED"


class SnapshotError(ValueError):
    """Snapshot malformado, inconsistente ou adulterado."""


# ------------------------------------------------------------------ entradas
@dataclass(frozen=True)
class ArtifactEntry:
    """Um artefato consumido: onde (caminho lógico), para quê (papel) e qual conteúdo (hash)."""

    path: str
    role: str
    status: str
    sha256: str | None = None
    byte_length: int | None = None

    def __post_init__(self) -> None:
        if not is_logical_path(self.path):
            raise SnapshotError("o caminho da entrada deve ser um caminho lógico normalizado")
        if not (isinstance(self.role, str) and _ROLE.match(self.role)):
            raise SnapshotError(f"papel inválido (esperado {ROLE_PATTERN})")
        if self.status == PRESENT:
            if not (isinstance(self.sha256, str) and _SHA256.match(self.sha256)):
                raise SnapshotError("entrada PRESENT exige sha256 com 64 hexadecimais minúsculos")
            if type(self.byte_length) is not int or self.byte_length < 0:
                raise SnapshotError("entrada PRESENT exige byte_length inteiro não negativo")
        elif self.status == ABSENT:
            if self.sha256 is not None or self.byte_length is not None:
                raise SnapshotError("entrada ABSENT não tem sha256 nem byte_length")
        else:
            raise SnapshotError(f"situação inválida: {self.status!r}")

    @classmethod
    def present(cls, path: str, role: str, digest: Digest) -> "ArtifactEntry":
        return cls(path, role, PRESENT, digest.digest, digest.byte_length)

    @classmethod
    def absent(cls, path: str, role: str) -> "ArtifactEntry":
        return cls(path, role, ABSENT)

    @classmethod
    def from_bytes(cls, path: str, role: str, data: bytes) -> "ArtifactEntry":
        """Entrada para conteúdo que está em memória (por exemplo, um registro já serializado)."""
        return cls.present(path, role, hash_bytes(data))

    def to_dict(self) -> dict:
        data: dict = {"path": self.path, "role": self.role, "status": self.status}
        if self.status == PRESENT:
            data["sha256"] = self.sha256
            data["byte_length"] = self.byte_length
        return data

    @classmethod
    def from_dict(cls, data: object) -> "ArtifactEntry":
        allowed = {"path", "role", "status", "sha256", "byte_length"}
        if not isinstance(data, dict) or not {"path", "role", "status"} <= set(data) or set(data) - allowed:
            raise SnapshotError("entrada de snapshot malformada")
        return cls(data["path"], data["role"], data["status"], data.get("sha256"), data.get("byte_length"))


class ArtifactSpec(NamedTuple):
    """O que declarar ao montar um snapshot a partir de um diretório."""

    path: str
    role: str


# ------------------------------------------------------------------ identidade
def _entries_digest(entries: Iterable[ArtifactEntry]) -> Digest:
    return hash_canonical_json([entry.to_dict() for entry in entries])


def _compute_snapshot_id(software_id: str | None, artifacts_digest: Digest) -> str:
    payload: dict = {
        "kind": "knowledge-snapshot",
        "identity_version": IDENTITY_VERSION,
        "artifacts_digest": artifacts_digest.digest,
    }
    if software_id is not None:
        payload["software_id"] = software_id
    return "snap:sha256:" + hash_canonical_json(payload).digest


def _valid_timestamp(text: str) -> bool:
    if not _TIMESTAMP.match(text):
        return False
    try:
        _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _check_text(name: str, value: object, limit: int) -> None:
    if not (isinstance(value, str) and 0 < len(value) <= limit) or _CONTROL.search(value):
        raise SnapshotError(f"{name} deve ser texto de 1 a {limit} caracteres, sem controles")
    if find_local_paths(value):
        raise SnapshotError(f"{name} carrega caminho local; use só caminhos lógicos")


# ------------------------------------------------------------------ snapshot
@dataclass(frozen=True)
class KnowledgeSnapshot:
    snapshot_id: str
    entries: tuple[ArtifactEntry, ...]
    artifacts_digest: Digest
    created_from_kind: str
    created_from_label: str
    generator: str
    software_id: str | None = None
    parent_snapshot_id: str | None = None
    created_at: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if not all(isinstance(entry, ArtifactEntry) for entry in self.entries):
            raise SnapshotError("entries deve conter ArtifactEntry")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            raise SnapshotError("entradas devem estar ordenadas por caminho, sem repetição")
        if len({path.casefold() for path in paths}) != len(paths):
            raise SnapshotError("caminhos que só diferem em maiúsculas e minúsculas não são portáteis")
        if self.software_id is not None and not (isinstance(self.software_id, str) and _SOFTWARE_ID.match(self.software_id)):
            raise SnapshotError("software_id inválido")
        if self.created_from_kind not in CREATED_FROM_KINDS:
            raise SnapshotError(f"created_from.kind deve ser um de {', '.join(CREATED_FROM_KINDS)}")
        _check_text("created_from.label", self.created_from_label, 200)
        _check_text("generation.generator", self.generator, 100)
        if self.notes is not None:
            _check_text("generation.notes", self.notes, 500)
        if self.created_at is not None and not (isinstance(self.created_at, str) and _valid_timestamp(self.created_at)):
            raise SnapshotError("generation.created_at deve ser date-time RFC 3339")
        if self.parent_snapshot_id is not None:
            if not (isinstance(self.parent_snapshot_id, str) and _SNAPSHOT_ID.match(self.parent_snapshot_id)):
                raise SnapshotError("parent_snapshot_id inválido")
            if self.parent_snapshot_id == self.snapshot_id:
                raise SnapshotError("um snapshot não pode ser pai de si mesmo")
        if not isinstance(self.artifacts_digest, Digest) or self.artifacts_digest != _entries_digest(self.entries):
            raise SnapshotError("artifacts_digest não confere com as entradas")
        if self.snapshot_id != _compute_snapshot_id(self.software_id, self.artifacts_digest):
            raise SnapshotError("snapshot_id não confere com o conteúdo (adulterado ou de outra versão de identidade)")

    def entry(self, path: str) -> ArtifactEntry | None:
        for candidate in self.entries:
            if candidate.path == path:
                return candidate
        return None

    def to_dict(self) -> dict:
        generation: dict = {"generator": self.generator}
        if self.created_at is not None:
            generation["created_at"] = self.created_at
        if self.notes is not None:
            generation["notes"] = self.notes
        data: dict = {
            "schema": SCHEMA_ID,
            "snapshot_id": self.snapshot_id,
            "identity_version": IDENTITY_VERSION,
        }
        if self.software_id is not None:
            data["software_id"] = self.software_id
        data["created_from"] = {"kind": self.created_from_kind, "label": self.created_from_label}
        data["entries"] = [entry.to_dict() for entry in self.entries]
        data["artifacts_digest"] = self.artifacts_digest.to_dict()
        if self.parent_snapshot_id is not None:
            data["parent_snapshot_id"] = self.parent_snapshot_id
        data["generation"] = generation
        return data


@functools.lru_cache(maxsize=1)
def _validator() -> Validator:
    return Validator(Registry())


def validate_snapshot_dict(data: object) -> list[str]:
    """Violações do contrato JSON (lista vazia = válido). Não recalcula hashes: ver `snapshot_from_dict`."""
    return _validator().validate(data, SCHEMA_NAME)


def _parent_id(parent: "KnowledgeSnapshot | str | None", software_id: str | None) -> str | None:
    if parent is None:
        return None
    if isinstance(parent, KnowledgeSnapshot):
        if parent.software_id != software_id:
            raise SnapshotError("o snapshot pai é de outro software (ou de nenhum): um software por vez")
        return parent.snapshot_id
    return parent


def build_snapshot(
    entries: Iterable[ArtifactEntry],
    *,
    created_from_kind: str,
    created_from_label: str,
    software_id: str | None = None,
    parent: "KnowledgeSnapshot | str | None" = None,
    created_at: str | None = None,
    notes: str | None = None,
    generator: str | None = None,
) -> KnowledgeSnapshot:
    """Monta um snapshot a partir de entradas já calculadas. A ordem de `entries` não importa."""
    items = list(entries)
    if not all(isinstance(entry, ArtifactEntry) for entry in items):
        raise SnapshotError("entries deve conter ArtifactEntry")
    ordered = tuple(sorted(items, key=lambda entry: entry.path))
    digest = _entries_digest(ordered)
    return KnowledgeSnapshot(
        snapshot_id=_compute_snapshot_id(software_id, digest),
        entries=ordered,
        artifacts_digest=digest,
        created_from_kind=created_from_kind,
        created_from_label=created_from_label,
        generator=generator if generator is not None else DEFAULT_GENERATOR,
        software_id=software_id,
        parent_snapshot_id=_parent_id(parent, software_id),
        created_at=created_at,
        notes=notes,
    )


def snapshot_directory(
    local_root: str | os.PathLike[str],
    specs: Iterable[ArtifactSpec | tuple[str, str]],
    *,
    label: str,
    software_id: str | None = None,
    parent: "KnowledgeSnapshot | str | None" = None,
    created_at: str | None = None,
    notes: str | None = None,
    generator: str | None = None,
) -> KnowledgeSnapshot:
    """Snapshot dos artefatos declarados sob `local_root`. A raiz local nunca entra no resultado.

    Arquivo inexistente vira `ABSENT`. Diretório no lugar de arquivo é erro. Se um arquivo existe e não
    pode ser lido, o erro do sistema propaga: não se produz snapshot a partir de conhecimento parcial.
    """
    root = Path(local_root)
    entries: list[ArtifactEntry] = []
    for path, role in specs:
        logical = normalize_logical_path(path)
        target = resolve_logical(root, logical)
        location = to_fs_path(target)
        if os.path.isfile(location):
            entries.append(ArtifactEntry.present(logical, role, hash_file(target)))
        elif os.path.exists(location):
            raise SnapshotError(f"{logical}: existe e não é um arquivo regular")
        else:
            entries.append(ArtifactEntry.absent(logical, role))
    return build_snapshot(
        entries,
        created_from_kind="directory",
        created_from_label=label,
        software_id=software_id,
        parent=parent,
        created_at=created_at,
        notes=notes,
        generator=generator,
    )


def snapshot_from_dict(data: object) -> KnowledgeSnapshot:
    """Reconstrói e VERIFICA um snapshot: schema, ordem, hash das entradas e `snapshot_id`."""
    errors = validate_snapshot_dict(data)
    if errors:
        raise SnapshotError("snapshot fora do contrato: " + "; ".join(errors[:5]))
    created, generation = data["created_from"], data["generation"]
    return KnowledgeSnapshot(
        snapshot_id=data["snapshot_id"],
        entries=tuple(ArtifactEntry.from_dict(item) for item in data["entries"]),
        artifacts_digest=Digest.from_dict(data["artifacts_digest"]),
        created_from_kind=created["kind"],
        created_from_label=created["label"],
        generator=generation["generator"],
        software_id=data.get("software_id"),
        parent_snapshot_id=data.get("parent_snapshot_id"),
        created_at=generation.get("created_at"),
        notes=generation.get("notes"),
    )


def save_snapshot(path: str | os.PathLike[str], snapshot: KnowledgeSnapshot) -> WriteResult:
    """Grava o snapshot em JSON canônico legível, de forma atômica."""
    return atomic_write_json(path, snapshot.to_dict())


def load_snapshot(path: str | os.PathLike[str]) -> KnowledgeSnapshot:
    """Lê com `loads_strict` (sem chave duplicada) e verifica o snapshot por inteiro."""
    with open(to_fs_path(path), "rb") as handle:
        return snapshot_from_dict(loads_strict(handle.read()))


# ------------------------------------------------------------------ deriva e verificação
@dataclass(frozen=True)
class SnapshotChange:
    path: str
    kind: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class SnapshotDiff:
    changes: tuple[SnapshotChange, ...]

    @property
    def same(self) -> bool:
        return not self.changes


def _state(entry: ArtifactEntry) -> str:
    return ABSENT if entry.status == ABSENT else f"sha256:{entry.sha256}"


def diff_snapshots(old: KnowledgeSnapshot, new: KnowledgeSnapshot) -> SnapshotDiff:
    """O que mudou entre dois snapshots do mesmo software, em ordem de caminho.

    `ADDED`/`REMOVED`: o artefato entrou ou saiu da lista. `MODIFIED`: o hash mudou.
    `APPEARED`/`DISAPPEARED`: passou de ausente a presente ou o contrário. `ROLE_CHANGED`: só o papel mudou.
    """
    if old.software_id != new.software_id:
        raise SnapshotError("snapshots de softwares diferentes não se comparam")
    before = {entry.path: entry for entry in old.entries}
    after = {entry.path: entry for entry in new.entries}
    changes: list[SnapshotChange] = []
    for path in sorted(set(before) | set(after)):
        x, y = before.get(path), after.get(path)
        if x is None:
            changes.append(SnapshotChange(path, ADDED, None, _state(y)))
        elif y is None:
            changes.append(SnapshotChange(path, REMOVED, _state(x), None))
        else:
            if (x.status, x.sha256) != (y.status, y.sha256):
                if x.status == ABSENT:
                    kind = APPEARED
                elif y.status == ABSENT:
                    kind = DISAPPEARED
                else:
                    kind = MODIFIED
                changes.append(SnapshotChange(path, kind, _state(x), _state(y)))
            if x.role != y.role:
                changes.append(SnapshotChange(path, ROLE_CHANGED, x.role, y.role))
    return SnapshotDiff(tuple(changes))


def verify_snapshot(
    snapshot: KnowledgeSnapshot,
    local_root: str | os.PathLike[str],
    *,
    detect_duplicates: bool = False,
    undeclared_within: Iterable[str] = (),
) -> IntegrityReport:
    """Confere o disco contra o snapshot. Chame `.raise_if_failed()` quando a evidência precisa estar congelada."""
    expected = [
        expect_present(entry.path, entry.sha256, entry.byte_length) if entry.status == PRESENT else expect_absent(entry.path)
        for entry in snapshot.entries
    ]
    return verify_sources(local_root, expected, detect_duplicates=detect_duplicates, undeclared_within=undeclared_within)
