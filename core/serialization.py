"""JSON canônico do agente. Contrato próprio: `mr-canonical-json/1`.

Mesmo valor semântico -> mesmos bytes, qualquer que seja a ordem de inserção das chaves, a execução,
a máquina ou o modelo de linguagem. Nada aqui lê relógio, ambiente ou configuração local.

Este contrato NÃO é o RFC 8785 (JCS) e não deve ser descrito como tal. Diferenças deliberadas:
chaves ordenadas por ponto de código Unicode (o JCS ordena por unidade UTF-16), números escritos
pela representação mais curta que reconstrói o mesmo `float` do Python, e os tipos `int` e `float`
continuam distintos.

Regras (valem para a forma compacta e para a forma legível):

- Codificação: UTF-8, sem BOM. Caracteres não ASCII saem literais; só o que o JSON obriga é escapado
  (`"`, `\\` e controles U+0000 a U+001F, com `\\uXXXX` em minúsculas quando não há forma curta).
- Chaves: sempre `str`, ordenadas por ponto de código. Chave de outro tipo é rejeitada (nada de
  `1` virar `"1"` e colidir com a chave `"1"`).
- Tipos aceitos: `dict`, `list`, `tuple` (vira array), `str`, `int`, `float` finito, `bool`, `None`.
  Escalares são aceitos só pelo tipo exato (subclasses como enums e tipos de bibliotecas externas
  são rejeitadas; converta antes). Qualquer outro tipo é rejeitado, em particular `set`, `bytes`,
  `datetime`, `Path` e `Decimal`.
- Números: `int` em decimal, de precisão arbitrária. `float` pela representação mais curta que
  reconstrói o mesmo valor; `NaN` e infinitos são rejeitados. `1` e `1.0` produzem bytes diferentes e
  `-0.0` é mantido como `-0.0`. Leitores em outras linguagens podem perder precisão em inteiros
  acima de 2^53: este contrato não trata disso.
- Texto: não há normalização Unicode (NFC e NFD são valores diferentes). Substitutos isolados
  (surrogates), que não existem em UTF-8, são rejeitados.
- Profundidade máxima: 100 níveis. Isso também barra referência circular.
- Forma compacta (`canonical_bytes`): sem espaço algum, sem quebra de linha final. É a forma que
  entra em hash e em identidade.
- Forma legível (`canonical_pretty_bytes`): recuo de 2 espaços, separadores `,` e `: `, quebras `\\n`
  (nunca CRLF) e uma quebra de linha final. É a forma para arquivos que pessoas revisam em diff.

As duas formas representam o mesmo valor. O hash de uma e o hash da outra são diferentes por construção.
"""
from __future__ import annotations

import json
import math

CONTRACT = "mr-canonical-json/1"
MAX_DEPTH = 100
_UTF8_BOM = b"\xef\xbb\xbf"


class CanonicalJsonError(ValueError):
    """O valor não cabe no contrato canônico, ou o texto lido o viola."""


def _where(path: str) -> str:
    return path or "$"


def _check(value: object, path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise CanonicalJsonError(f"{_where(path)}: profundidade acima de {MAX_DEPTH} (ou referência circular)")
    kind = type(value)
    if value is None or kind is bool or kind is int:
        return
    if kind is float:
        if not math.isfinite(value):
            raise CanonicalJsonError(f"{_where(path)}: número não finito ({value!r}) não é JSON")
        return
    if kind is str:
        if not value.isascii():
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalJsonError(f"{_where(path)}: texto com substituto Unicode isolado") from exc
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise CanonicalJsonError(f"{_where(path)}: chave de tipo {type(key).__name__}; só str é aceito")
            _check(key, f"{path}.<chave>", depth + 1)
            _check(item, f"{path}.{key}", depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]", depth + 1)
        return
    raise CanonicalJsonError(f"{_where(path)}: tipo {kind.__name__} não é aceito")


def _dumps(value: object, **options: object) -> str:
    _check(value, "", 0)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, **options)
    except ValueError as exc:  # por exemplo, inteiro com dígitos além do limite do interpretador
        raise CanonicalJsonError(f"valor não serializável: {exc}") from exc


def canonical_dumps(value: object) -> str:
    """Forma compacta como texto. Levanta `CanonicalJsonError` se o valor viola o contrato."""
    return _dumps(value, separators=(",", ":"))


def canonical_bytes(value: object) -> bytes:
    """Forma compacta em UTF-8, sem quebra de linha final. É a entrada de todo hash de valor."""
    return canonical_dumps(value).encode("utf-8")


def canonical_pretty_bytes(value: object) -> bytes:
    """Forma legível em UTF-8: recuo de 2, chaves ordenadas, LF, uma quebra de linha final."""
    return (_dumps(value, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")


def _reject_constant(name: str) -> object:
    raise CanonicalJsonError(f"constante {name} não é JSON válido neste contrato")


def _parse_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise CanonicalJsonError(f"número fora do intervalo finito: {text}")
    return number


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, item in pairs:
        if key in result:
            raise CanonicalJsonError(f"chave duplicada: {key!r}")
        result[key] = item
    return result


def loads_strict(data: bytes | str) -> object:
    """Leitura estrita: UTF-8 sem BOM, sem chave duplicada, sem NaN nem infinito.

    `json.loads` aceita tudo isso em silêncio (a última chave vence). Para quem depende de
    "o que li é exatamente o que foi escrito", o silêncio é um defeito.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if raw.startswith(_UTF8_BOM):
            raise CanonicalJsonError("BOM UTF-8 não é aceito")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalJsonError("bytes não são UTF-8 válido") from exc
    elif isinstance(data, str):
        text = data
    else:
        raise TypeError("loads_strict aceita bytes ou str")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except CanonicalJsonError:
        raise
    except ValueError as exc:  # JSONDecodeError e estouro de dígitos em inteiros
        raise CanonicalJsonError(f"JSON inválido: {exc}") from exc
