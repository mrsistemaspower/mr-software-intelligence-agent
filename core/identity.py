"""Identidade determinística de artefatos derivados.

    artifact_id = "art:" + tipo + ":sha256:" + hash( payload canônico )

O payload tem entradas fechadas e explícitas:

- `artifact_type` e `schema_version` do artefato;
- `inputs`: nome lógico -> hash de cada fonte consumida;
- `parameters`: só o que muda o resultado (JSON canônico, ver `core.serialization`);
- `snapshot_id` do estado de conhecimento consumido, quando houver;
- `software_id`, quando o artefato pertence a um software.

O id NÃO depende, e a API nem oferece argumento para isso, de: UUID ou número aleatório, relógio,
nome da máquina, caminho absoluto local, provedor ou modelo de linguagem. Quem produziu o artefato, quando
e com qual modelo é metadado de geração e fica ao lado do artefato, nunca dentro do payload.

Defesas contra vazamento acidental pelo único campo livre (`parameters`): chaves como `timestamp`, `uuid`,
`hostname` ou `provider` são recusadas, e qualquer texto ou chave que pareça caminho local também.
São barreiras contra descuido, não uma prova: um UUID escrito num valor de texto passa. A garantia
forte é estrutural: o que não é argumento não entra no id.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .hashing import Digest, digest_reference, hash_canonical_json
from .paths import assert_portable
from .serialization import canonical_bytes, loads_strict
from .snapshot import SNAPSHOT_ID_PATTERN, SOFTWARE_ID_PATTERN, KnowledgeSnapshot

IDENTITY_VERSION = 1
TYPE_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"

FORBIDDEN_PARAMETER_KEYS = frozenset({
    "timestamp", "created_at", "generated_at", "updated_at",
    "uuid", "guid", "nonce", "random",
    "hostname", "machine", "machine_name", "computer_name",
    "provider", "llm", "llm_provider", "llm_model",
})

_TYPE = re.compile(TYPE_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)
_SOFTWARE_ID = re.compile(SOFTWARE_ID_PATTERN)
_SNAPSHOT_ID = re.compile(SNAPSHOT_ID_PATTERN)


@dataclass(frozen=True)
class ArtifactIdentity:
    """O id e o payload exato que o originou, para que a identidade possa ser explicada e reconferida."""

    artifact_id: str
    digest: Digest
    payload: dict

    def __post_init__(self) -> None:
        recomputed = hash_canonical_json(self.payload)
        expected = f"art:{self.payload['artifact_type']}:sha256:{recomputed.digest}"
        if self.digest != recomputed or self.artifact_id != expected:
            raise ValueError("artifact_id não confere com o payload")

    def __str__(self) -> str:
        return self.artifact_id


def _forbidden_keys(node: object, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, item in node.items():
            if key.casefold() in FORBIDDEN_PARAMETER_KEYS:
                found.append(key)
            _forbidden_keys(item, found)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _forbidden_keys(item, found)


def _snapshot_ref(snapshot: "KnowledgeSnapshot | str | None", software_id: str | None) -> tuple[str | None, str | None]:
    if snapshot is None:
        return None, software_id
    if isinstance(snapshot, KnowledgeSnapshot):
        if software_id is not None and snapshot.software_id is not None and software_id != snapshot.software_id:
            raise ValueError("o snapshot é de outro software: um software por vez")
        return snapshot.snapshot_id, software_id if software_id is not None else snapshot.software_id
    if isinstance(snapshot, str) and _SNAPSHOT_ID.match(snapshot):
        return snapshot, software_id
    raise ValueError("snapshot deve ser um KnowledgeSnapshot ou um snapshot_id")


def artifact_identity(
    artifact_type: str,
    schema_version: str,
    *,
    inputs: "dict[str, Digest | str] | None" = None,
    parameters: "dict | None" = None,
    snapshot: "KnowledgeSnapshot | str | None" = None,
    software_id: str | None = None,
) -> ArtifactIdentity:
    """Calcula a identidade de um artefato derivado. Mesmas entradas, mesmo id, sempre."""
    if not (isinstance(artifact_type, str) and len(artifact_type) <= 64 and _TYPE.match(artifact_type)):
        raise ValueError(f"artifact_type inválido (esperado {TYPE_PATTERN}, até 64 caracteres)")
    if not (isinstance(schema_version, str) and _VERSION.match(schema_version)):
        raise ValueError(f"schema_version inválido (esperado {VERSION_PATTERN})")
    if software_id is not None and not (isinstance(software_id, str) and _SOFTWARE_ID.match(software_id)):
        raise ValueError("software_id inválido")
    snapshot_id, software = _snapshot_ref(snapshot, software_id)

    references: dict[str, str] = {}
    for name, value in (inputs or {}).items():
        if not (isinstance(name, str) and name):
            raise ValueError("nomes de entrada devem ser texto não vazio")
        references[name] = digest_reference(value)

    if parameters is not None and not isinstance(parameters, dict):
        raise ValueError("parameters deve ser um dict")
    # Recusa tipos e valores fora do JSON canônico e devolve uma cópia desacoplada do dict do chamador.
    params = loads_strict(canonical_bytes({} if parameters is None else parameters))
    leaked: list[str] = []
    _forbidden_keys(params, leaked)
    if leaked:
        raise ValueError("parâmetros não podem variar com tempo, acaso, máquina ou provedor: " + ", ".join(sorted(set(leaked))))

    payload: dict = {
        "kind": "artifact-identity",
        "identity_version": IDENTITY_VERSION,
        "artifact_type": artifact_type,
        "schema_version": schema_version,
        "inputs": references,
        "parameters": params,
    }
    if software is not None:
        payload["software_id"] = software
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    assert_portable(payload, "identidade de artefato")

    digest = hash_canonical_json(payload)
    return ArtifactIdentity(f"art:{artifact_type}:sha256:{digest.digest}", digest, payload)
