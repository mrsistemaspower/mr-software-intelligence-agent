"""AnalysisSignal: o contrato portátil de uma observação que pode mudar a rota de revisão. Genérico, sem software e sem política.

Uma análise (de código, de documento, de esquema de banco, de rastro de execução, de configuração, de API, de captura de tela ou
de qualquer outro assunto) emite SINAIS. O roteador (`core.review_routing`) lê os sinais e uma política e decide a rota. Este módulo
só define o que um sinal É e o vocabulário fechado que o cerca; nenhuma decisão mora aqui.

Eixos que não se confundem (nenhum é derivado de outro):

- `criticality` (low / medium / high / critical): o quanto o ponto em dúvida importa para o propósito do assunto;
- `confidence` (low / medium / high): a certeza que a análise tem sobre a afirmação. Registrada para observabilidade; baixa
  confiança não é revisão humana, e alta confiança não é segurança (o roteador nunca usa a confiança para suprimir um sinal);
- `impact_class` (por exemplo informational, financial, security, irreversible_action): o que a afirmação faz se estiver errada. Pode
  vir declarada, ou ser derivada pela política dos `effects` (etiquetas genéricas como `deletes-data`). A política dá a cada classe um piso de criticidade;
- disponibilidade da evidência: a EXISTÊNCIA de `evidence` (referência + hash) no sinal. Dúvida de leitura sobre evidência que existe
  é candidata a `AI_RECHECK`; evidência que não existe é `SOURCE_GAP`; evidência que existe e se contradiz sem que a IA possa
  decidir é `HUMAN_DECISION`. Essa distinção é a invariante central do roteamento.

A taxonomia de famílias é dado, não código. `BASE_FAMILIES` lista as famílias que a política padrão conhece; estender é acrescentar
uma regra à política (o roteador não tem `if` por família). `qualifier` refina uma família sem criar outra (por exemplo o tipo de
conflito, ou o escopo de uma preocupação de privacidade).

Um sinal carrega referências e hashes de evidência, nunca o conteúdo bruto, nem caminho local. `signal_id` deriva do conteúdo
(`core.identity`) e é conferido ao construir e ao carregar: mesmo sinal, mesmo id, sempre.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .epistemics import STATUSES as EPISTEMIC_STATUSES
from .epistemics import band as confidence_band  # noqa: F401 - reexportado: a faixa de confiança é calculada, nunca armazenada
from .identity import artifact_identity
from .model_task import (
    BOUNDARIES,
    SCHEMA_URN_PREFIX,
    SHA256_PATTERN,
    EvidenceRef,
    ModelContractError,
    check_choice,
    check_name,
    check_ref,
    check_text,
    schema_violations,
)
from .paths import find_local_paths

SIGNAL_KIND = "analysis-signal"
SIGNAL_SCHEMA = f"{SCHEMA_URN_PREFIX}analysis-signal:1"
SIGNAL_ARTIFACT = "analysis.signal"
IDENTITY_VERSION = "1"

CRITICALITIES = ("low", "medium", "high", "critical")
CONFIDENCE_BANDS = ("low", "medium", "high")
# O que a política pode pedir de um sinal. OBSERVE: só registrar (sinal secundário, sem efeito de rota).
ACTIONS = ("OBSERVE", "SAMPLE_QA", "AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION")
REVIEW_CLASSES = ("AUTO_PASS", "SAMPLE_QA", "AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION")
# Para que serve a decisão humana. A política liga cada tipo às suposições proibidas.
DECISION_KINDS = ("resolve_conflict", "validate_claim", "apply_policy", "assess_concern", "resolve_residue")
# Tipos genéricos de fonte a obter. Nomes de adaptadores específicos moram nos adaptadores, nunca aqui.
SOURCE_TYPES = (
    "documentation", "runtime", "database", "source-code", "screenshot", "filesystem", "release-notes", "human-expert", "external-system",
)
# Tipo genérico -> tipo de fonte registrado em `common#/$defs/sourceKind` (None: ainda não há adaptador de fonte correspondente).
SOURCE_TYPE_TO_KIND = {
    "documentation": "documentation",
    "runtime": "runtime",
    "database": "database-schema",
    "source-code": "source-code",
    "screenshot": "screenshot",
    "filesystem": "filesystem",
    "release-notes": "release-notes",
    "human-expert": "human-expert",
    "external-system": None,
}
# Tipo registrado -> tipo genérico. `wiki` é documentação.
SOURCE_KIND_TO_TYPE = {
    "screenshot": "screenshot",
    "filesystem": "filesystem",
    "documentation": "documentation",
    "wiki": "documentation",
    "source-code": "source-code",
    "database-schema": "database",
    "runtime": "runtime",
    "release-notes": "release-notes",
    "human-expert": "human-expert",
}

# Famílias que a política padrão conhece. Dado, não código: o roteador não ramifica por família.
FAMILY_LOW_CONFIDENCE = "LOW_CONFIDENCE"
FAMILY_AMBIGUOUS = "AMBIGUOUS_INTERPRETATION"
FAMILY_SELF_AUDIT = "SELF_AUDIT_REVIEW"
FAMILY_MISSING_EVIDENCE = "MISSING_EVIDENCE"
FAMILY_TRUNCATED_EVIDENCE = "TRUNCATED_EVIDENCE"
FAMILY_UNKNOWN_RELATION = "UNKNOWN_RELATION"
FAMILY_STRUCTURE_ONLY = "STRUCTURE_ONLY"
FAMILY_CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
FAMILY_POLICY_CONFLICT = "POLICY_CONFLICT"
FAMILY_UNVERIFIED_HIGH_IMPACT = "UNVERIFIED_HIGH_IMPACT_CLAIM"
FAMILY_PRIVACY_OR_SECURITY = "PRIVACY_OR_SECURITY_CONCERN"
BASE_FAMILIES = (
    FAMILY_LOW_CONFIDENCE, FAMILY_AMBIGUOUS, FAMILY_SELF_AUDIT,
    FAMILY_MISSING_EVIDENCE, FAMILY_TRUNCATED_EVIDENCE, FAMILY_UNKNOWN_RELATION, FAMILY_STRUCTURE_ONLY,
    FAMILY_CONFLICTING_EVIDENCE, FAMILY_POLICY_CONFLICT,
    FAMILY_UNVERIFIED_HIGH_IMPACT, FAMILY_PRIVACY_OR_SECURITY,
)

FAMILY_PATTERN = r"^[A-Z][A-Z0-9_]{1,63}$"
QUALIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
EFFECT_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
IMPACT_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

MAX_EFFECTS = 20
MAX_EVIDENCE = 50
MAX_POSITIONS = 10

_FAMILY = re.compile(FAMILY_PATTERN)
_QUALIFIER = re.compile(QUALIFIER_PATTERN)
_EFFECT = re.compile(EFFECT_PATTERN)
_IMPACT = re.compile(IMPACT_PATTERN)
_SHA256 = re.compile(SHA256_PATTERN)
_MAX_ISSUES = 8


class ReviewContractError(ModelContractError):
    """Sinal, política, decisão ou artefato de revisão fora das regras. `code` é estável e serve para decidir."""


def _err(code: str, message: str) -> ReviewContractError:
    return ReviewContractError(code, message)


# ----------------------------------------------------------------------------------------------------- vocabulário pequeno
def criticality_rank(value: str) -> int:
    """Posição de uma criticidade na ordem crescente (low = 0)."""
    return CRITICALITIES.index(check_choice(value, CRITICALITIES, "criticality"))


def max_criticality(*values: str) -> str:
    """A maior das criticidades dadas (pelo menos uma)."""
    if not values:
        raise _err("INVALID_CRITICALITY", "max_criticality exige ao menos um valor")
    return max(values, key=criticality_rank)


def check_family(value: object, what: str = "family") -> str:
    if not (isinstance(value, str) and _FAMILY.match(value)):
        raise _err("INVALID_FAMILY", f"{what} inválida (esperado {FAMILY_PATTERN})")
    return value


def check_qualifier(value: object, what: str = "qualifier") -> str:
    if not (isinstance(value, str) and _QUALIFIER.match(value)):
        raise _err("INVALID_QUALIFIER", f"{what} inválido (esperado {QUALIFIER_PATTERN})")
    return value


def check_effect(value: object) -> str:
    if not (isinstance(value, str) and len(value) <= 64 and _EFFECT.match(value)):
        raise _err("INVALID_EFFECT", f"efeito inválido (esperado {EFFECT_PATTERN}, até 64 caracteres)")
    return value


def check_impact_class(value: object, what: str = "impact_class") -> str:
    if not (isinstance(value, str) and _IMPACT.match(value)):
        raise _err("INVALID_IMPACT", f"{what} inválida (esperado {IMPACT_PATTERN})")
    return value


def source_type_for_kind(kind: str) -> str:
    """Tipo genérico de fonte para um tipo registrado em `sourceKind`. Tipo desconhecido é recusado, não adivinhado."""
    if kind not in SOURCE_KIND_TO_TYPE:
        raise _err("INVALID_SOURCE_KIND", f"tipo de fonte registrado desconhecido: {kind!r}")
    return SOURCE_KIND_TO_TYPE[kind]


def source_kind_for_type(source_type: str) -> "str | None":
    """Tipo registrado em `sourceKind` para um tipo genérico, ou `None` quando ainda não há correspondente."""
    return SOURCE_TYPE_TO_KIND[check_choice(source_type, SOURCE_TYPES, "tipo de fonte")]


def check_source_types(values: object, what: str = "recommended_source_types") -> "tuple[str, ...]":
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise _err("INVALID_SOURCE_TYPES", f"{what} deve ser uma lista")
    return tuple(sorted({check_choice(item, SOURCE_TYPES, "tipo de fonte") for item in values}))  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------------------------ evidência
@dataclass(frozen=True)
class EvidenceBinding:
    """Referência a uma evidência e o hash do conteúdo que a análise consultou. Nunca o conteúdo. Comparável por valor."""

    ref: str
    sha256: str

    def __post_init__(self) -> None:
        check_ref(self.ref, "ref de evidência")
        if not (isinstance(self.sha256, str) and _SHA256.match(self.sha256)):
            raise _err("INVALID_HASH", "sha256 da evidência deve ter 64 hexadecimais minúsculos")

    def to_dict(self) -> dict:
        return {"ref": self.ref, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: object) -> "EvidenceBinding":
        if not isinstance(data, dict) or set(data) != {"ref", "sha256"}:
            raise _err("MALFORMED", "evidência malformada (esperado ref e sha256)")
        return cls(data["ref"], data["sha256"])

    @classmethod
    def of(cls, evidence: EvidenceRef) -> "EvidenceBinding":
        """O vínculo de uma evidência autorizada (`EvidenceRef`)."""
        return cls(evidence.ref, evidence.sha256)


def bindings_tuple(items: object, what: str = "evidence", limit: int = MAX_EVIDENCE) -> "tuple[EvidenceBinding, ...]":
    """Vínculos normalizados: ordenados por ref, sem repetição, e a mesma ref nunca com dois hashes."""
    if isinstance(items, (str, bytes, dict)) or not hasattr(items, "__iter__"):
        raise _err("INVALID_EVIDENCE", f"{what} deve ser uma lista")
    found: dict[str, EvidenceBinding] = {}
    for item in items:  # type: ignore[union-attr]
        if isinstance(item, EvidenceRef):
            item = EvidenceBinding.of(item)
        elif isinstance(item, dict):
            item = EvidenceBinding.from_dict(item)
        if not isinstance(item, EvidenceBinding):
            raise _err("INVALID_EVIDENCE", f"{what} deve conter EvidenceBinding, EvidenceRef ou objeto {{ref, sha256}}")
        if item.ref in found and found[item.ref] != item:
            raise _err("EVIDENCE_HASH_CONFLICT", f"a evidência {item.ref} aparece com dois hashes diferentes")
        found[item.ref] = item
    if len(found) > limit:
        raise _err("TOO_MANY_ITEMS", f"{what} excede {limit} itens")
    return tuple(found[ref] for ref in sorted(found))


# -------------------------------------------------------------------------------------- o que falta / o que se contradiz
@dataclass(frozen=True)
class SourceNeed:
    """O que falta para fechar uma dúvida, por que importa e que tipos genéricos de fonte poderiam trazê-lo."""

    missing_information: str
    why_needed: str
    recommended_source_types: "tuple[str, ...]" = ()

    def __post_init__(self) -> None:
        check_text(self.missing_information, "missing_information")
        check_text(self.why_needed, "why_needed")
        object.__setattr__(self, "recommended_source_types", check_source_types(self.recommended_source_types))

    def to_dict(self) -> dict:
        return {
            "missing_information": self.missing_information,
            "why_needed": self.why_needed,
            "recommended_source_types": list(self.recommended_source_types),
        }

    @classmethod
    def from_dict(cls, data: object) -> "SourceNeed":
        if not isinstance(data, dict) or set(data) != {"missing_information", "why_needed", "recommended_source_types"}:
            raise _err("MALFORMED", "necessidade de fonte malformada")
        return cls(data["missing_information"], data["why_needed"], tuple(data["recommended_source_types"]))


@dataclass(frozen=True)
class Position:
    """Uma das posições de um conflito: a afirmação (ref), o resumo e, se souber, de onde vem (por exemplo observed, human)."""

    ref: str
    summary: str
    origin: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.ref, "ref da posição")
        check_text(self.summary, "summary da posição")
        if self.origin is not None:
            check_name(self.origin, "origin da posição")

    def to_dict(self) -> dict:
        data = {"ref": self.ref, "summary": self.summary}
        if self.origin is not None:
            data["origin"] = self.origin
        return data

    @classmethod
    def from_dict(cls, data: object) -> "Position":
        if not isinstance(data, dict) or not {"ref", "summary"} <= set(data) or set(data) - {"ref", "summary", "origin"}:
            raise _err("MALFORMED", "posição malformada")
        return cls(data["ref"], data["summary"], data.get("origin"))


# ------------------------------------------------------------------------------------------------------------ o sinal
@dataclass(frozen=True)
class AnalysisSignal:
    """Um sinal de análise. Imutável; `signal_id` deriva do conteúdo e é conferido ao construir e ao carregar.

    `subject_ref`/`subject_kind` dizem de QUE artefato é o sinal (qualquer um: tela, documento, código, objeto de banco, rastro de
    execução, configuração, API, fluxo, regra, nota de versão...). `target_ref` diz o ponto dentro dele (padrão: o próprio assunto).
    `evidence` é a evidência que EXISTE e foi consultada: sem ela, uma dúvida de leitura não tem o que reler.
    """

    family: str
    subject_ref: str
    subject_kind: str
    summary: str
    criticality: str = "medium"
    target_ref: "str | None" = None
    qualifier: "str | None" = None
    confidence: "str | None" = None
    impact_class: "str | None" = None
    effects: "tuple[str, ...]" = ()
    evidence: "tuple[EvidenceBinding, ...]" = ()
    source_need: "SourceNeed | None" = None
    positions: "tuple[Position, ...]" = ()
    boundary_limit: "str | None" = None
    recommended_role: "str | None" = None
    signal_id: "str | None" = None

    def __post_init__(self) -> None:
        check_family(self.family)
        check_ref(self.subject_ref, "subject_ref")
        check_name(self.subject_kind, "subject_kind")
        check_text(self.summary, "summary")
        check_choice(self.criticality, CRITICALITIES, "criticality")
        if self.target_ref is None:
            object.__setattr__(self, "target_ref", self.subject_ref)
        else:
            check_ref(self.target_ref, "target_ref")
        if self.qualifier is not None:
            check_qualifier(self.qualifier)
        if self.confidence is not None:
            check_choice(self.confidence, CONFIDENCE_BANDS, "confidence")
        if self.impact_class is not None:
            check_impact_class(self.impact_class)

        effects = tuple(self.effects)
        if isinstance(self.effects, (str, bytes)):
            raise _err("INVALID_EFFECT", "effects deve ser uma lista")
        if len(effects) > MAX_EFFECTS:
            raise _err("TOO_MANY_ITEMS", f"no máximo {MAX_EFFECTS} efeitos")
        object.__setattr__(self, "effects", tuple(sorted({check_effect(item) for item in effects})))
        object.__setattr__(self, "evidence", bindings_tuple(self.evidence))

        if self.source_need is not None and not isinstance(self.source_need, SourceNeed):
            raise _err("INVALID_SOURCE_NEED", "source_need deve ser SourceNeed")
        positions = tuple(self.positions)
        if not all(isinstance(item, Position) for item in positions):
            raise _err("INVALID_POSITIONS", "positions deve conter Position")
        if len(positions) > MAX_POSITIONS:
            raise _err("TOO_MANY_ITEMS", f"no máximo {MAX_POSITIONS} posições")
        object.__setattr__(self, "positions", tuple(sorted(set(positions), key=lambda item: (item.ref, item.summary, item.origin or ""))))

        if self.boundary_limit is not None:
            check_choice(self.boundary_limit, BOUNDARIES, "boundary_limit")
        if self.recommended_role is not None:
            check_name(self.recommended_role, "recommended_role")
        if find_local_paths(self._body()):
            raise _err("LOCAL_PATH", "o sinal não pode carregar caminho local")

        identity = self._identity()
        if self.signal_id is not None and self.signal_id != identity:
            raise _err("ID_MISMATCH", "signal_id não confere com o conteúdo do sinal")
        object.__setattr__(self, "signal_id", identity)

    def _body(self) -> dict:
        body: dict = {
            "family": self.family,
            "subject": {"ref": self.subject_ref, "kind": self.subject_kind},
            "target_ref": self.target_ref,
            "summary": self.summary,
            "criticality": self.criticality,
        }
        if self.qualifier is not None:
            body["qualifier"] = self.qualifier
        if self.confidence is not None:
            body["confidence"] = self.confidence
        if self.impact_class is not None:
            body["impact_class"] = self.impact_class
        if self.effects:
            body["effects"] = list(self.effects)
        if self.source_need is not None:
            body["source_need"] = self.source_need.to_dict()
        if self.positions:
            body["positions"] = [item.to_dict() for item in self.positions]
        if self.boundary_limit is not None:
            body["boundary_limit"] = self.boundary_limit
        if self.recommended_role is not None:
            body["recommended_role"] = self.recommended_role
        return body

    def _identity(self) -> str:
        return artifact_identity(
            SIGNAL_ARTIFACT,
            IDENTITY_VERSION,
            inputs={item.ref: item.sha256 for item in self.evidence},
            parameters=self._body(),
        ).artifact_id

    @property
    def rule_key(self) -> str:
        """Chave de regra mais específica: `FAMILIA:qualificador` quando há qualificador, senão a família."""
        return f"{self.family}:{self.qualifier}" if self.qualifier is not None else self.family

    def to_dict(self) -> dict:
        data = {"kind": SIGNAL_KIND, "signal_id": self.signal_id, **self._body()}
        if self.evidence:
            data["evidence"] = [item.to_dict() for item in self.evidence]
        return data

    @classmethod
    def from_dict(cls, data: object) -> "AnalysisSignal":
        problems = schema_violations(data, SIGNAL_SCHEMA)
        if problems:
            raise _err("SCHEMA", "sinal fora do contrato: " + "; ".join(problems[:_MAX_ISSUES]))
        assert isinstance(data, dict)
        subject = data["subject"]
        need = data.get("source_need")
        return cls(
            family=data["family"],
            subject_ref=subject["ref"],
            subject_kind=subject["kind"],
            summary=data["summary"],
            criticality=data["criticality"],
            target_ref=data["target_ref"],
            qualifier=data.get("qualifier"),
            confidence=data.get("confidence"),
            impact_class=data.get("impact_class"),
            effects=tuple(data.get("effects", ())),
            evidence=tuple(EvidenceBinding.from_dict(item) for item in data.get("evidence", ())),
            source_need=SourceNeed.from_dict(need) if need is not None else None,
            positions=tuple(Position.from_dict(item) for item in data.get("positions", ())),
            boundary_limit=data.get("boundary_limit"),
            recommended_role=data.get("recommended_role"),
            signal_id=data["signal_id"],
        )


# ------------------------------------------------------------------------------------------- afirmação com efeito declarado
@dataclass(frozen=True)
class ClaimAssessment:
    """Uma afirmação da análise com os EFEITOS que ela atribui ao assunto e o status epistêmico que a sustenta.

    Existe para fechar uma lacuna: afirmações de alto impacto (efeito fiscal, financeiro, exclusão, transmissão externa) eram
    marcadas à mão. Aqui o adaptador de domínio só declara efeitos genéricos (`deletes-data`, `transmits-externally`...) e o status;
    a classe de impacto sai da política, e o roteador deriva o sinal `UNVERIFIED_HIGH_IMPACT_CLAIM` quando a política manda.
    Nada aqui conhece um software.
    """

    claim_ref: str
    statement: str
    status: str
    effects: "tuple[str, ...]" = ()
    impact_class: "str | None" = None
    criticality: str = "medium"
    evidence: "tuple[EvidenceBinding, ...]" = ()
    recommended_role: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.claim_ref, "claim_ref")
        check_text(self.statement, "statement")
        check_choice(self.status, EPISTEMIC_STATUSES, "status")
        if isinstance(self.effects, (str, bytes)):
            raise _err("INVALID_EFFECT", "effects deve ser uma lista")
        object.__setattr__(self, "effects", tuple(sorted({check_effect(item) for item in self.effects})))
        if self.impact_class is not None:
            check_impact_class(self.impact_class)
        check_choice(self.criticality, CRITICALITIES, "criticality")
        object.__setattr__(self, "evidence", bindings_tuple(self.evidence))
        if self.recommended_role is not None:
            check_name(self.recommended_role, "recommended_role")

    def as_signal(self, subject_ref: str, subject_kind: str) -> AnalysisSignal:
        """O sinal `UNVERIFIED_HIGH_IMPACT_CLAIM` desta afirmação. Se ele vale ou não é decisão da política, não daqui."""
        return AnalysisSignal(
            family=FAMILY_UNVERIFIED_HIGH_IMPACT,
            subject_ref=subject_ref,
            subject_kind=subject_kind,
            summary=self.statement,
            criticality=self.criticality,
            target_ref=self.claim_ref,
            impact_class=self.impact_class,
            effects=self.effects,
            evidence=self.evidence,
            recommended_role=self.recommended_role,
        )
