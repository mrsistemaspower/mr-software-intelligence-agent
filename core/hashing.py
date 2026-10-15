"""Hash determinístico: bytes, arquivos e valores JSON canônicos.

SHA-256 é o padrão e, por ora, o único algoritmo aceito. A lista é fechada de propósito: um algoritmo
fraco não entra por descuido, e quem precisar de outro muda o contrato de forma visível.

Duas perguntas diferentes, dois hashes diferentes:

- `hash_file` / `hash_bytes`: "estes são exatamente os bytes que li?" (integridade de fonte).
- `hash_canonical_json`: "este é o mesmo valor, independentemente de espaço e ordem de chaves?"
  (identidade de conteúdo estruturado). Usa a forma compacta de `core.serialization`.

Nenhuma função aqui conhece software, tela, caminho do Windows ou relógio.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from .paths import to_fs_path
from .serialization import canonical_bytes

DEFAULT_ALGORITHM = "sha256"
SUPPORTED_ALGORITHMS = ("sha256",)
CHUNK_SIZE = 1 << 20

_HEX_LENGTH = {"sha256": 64}
_BARE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^([a-z0-9]+):([0-9a-f]+)$")


def _check_algorithm(algorithm: str) -> None:
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"algoritmo de hash não suportado: {algorithm!r} (aceitos: {', '.join(SUPPORTED_ALGORITHMS)})")


@dataclass(frozen=True)
class Digest:
    """Resultado de um hash: algoritmo, resumo em hexadecimal minúsculo e tamanho em bytes."""

    algorithm: str
    digest: str
    byte_length: int

    def __post_init__(self) -> None:
        _check_algorithm(self.algorithm)
        expected = _HEX_LENGTH[self.algorithm]
        if not isinstance(self.digest, str) or not re.fullmatch(r"[0-9a-f]{%d}" % expected, self.digest):
            raise ValueError(f"resumo inválido para {self.algorithm}: esperado {expected} dígitos hexadecimais minúsculos")
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise ValueError("byte_length deve ser inteiro não negativo")

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.digest}"

    def to_dict(self) -> dict:
        return {"algorithm": self.algorithm, "digest": self.digest, "byte_length": self.byte_length}

    @classmethod
    def from_dict(cls, data: object) -> "Digest":
        if not isinstance(data, dict) or set(data) != {"algorithm", "digest", "byte_length"}:
            raise ValueError("digest deve ter exatamente algorithm, digest e byte_length")
        return cls(data["algorithm"], data["digest"], data["byte_length"])


def digest_reference(value: "Digest | str") -> str:
    """Forma textual `algoritmo:resumo` de um `Digest` ou de um texto equivalente.

    Aceita `sha256:<64 hex>` ou os 64 hexadecimais soltos (convenção dos registros de fonte, que
    guardam só SHA-256). Qualquer outra coisa levanta `ValueError`.
    """
    if isinstance(value, Digest):
        return str(value)
    if isinstance(value, str):
        if _BARE_SHA256.match(value):
            return f"sha256:{value}"
        match = _REF.match(value)
        if match:
            algorithm, hexdigest = match.groups()
            _check_algorithm(algorithm)
            if len(hexdigest) == _HEX_LENGTH[algorithm]:
                return value
    raise ValueError("referência de hash inválida: use um Digest, 'sha256:<64 hex>' ou 64 hexadecimais minúsculos")


def hash_bytes(data: bytes, algorithm: str = DEFAULT_ALGORITHM) -> Digest:
    """Hash de bytes. Texto (`str`) é recusado: quem codifica decide a codificação."""
    _check_algorithm(algorithm)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("hash_bytes exige bytes; codifique o texto antes (por exemplo, UTF-8)")
    view = memoryview(data)
    return Digest(algorithm, hashlib.new(algorithm, view).hexdigest(), view.nbytes)


def hash_file(path: str | os.PathLike[str], algorithm: str = DEFAULT_ALGORITHM, chunk_size: int = CHUNK_SIZE) -> Digest:
    """Hash de um arquivo, em blocos. O tamanho é o dos bytes realmente lidos, não o de `stat`."""
    _check_algorithm(algorithm)
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser positivo")
    hasher = hashlib.new(algorithm)
    total = 0
    with open(to_fs_path(path), "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            hasher.update(block)
            total += len(block)
    return Digest(algorithm, hasher.hexdigest(), total)


def hash_canonical_json(value: object, algorithm: str = DEFAULT_ALGORITHM) -> Digest:
    """Hash da forma compacta canônica do valor (ver `core.serialization`)."""
    return hash_bytes(canonical_bytes(value), algorithm)
