"""Roteamento de revisão: sinais + política -> `ReviewDecision`. Função pura e determinística; sem rede, arquivo, modelo, Git ou relógio.

O roteador responde QUANDO uma análise deve passar sozinha, voltar para uma releitura por IA, virar lacuna de fonte, exigir
decisão humana ou entrar em amostragem de qualidade. Quem EXECUTA cada rota (o executor de `AI_RECHECK`, o backlog de fontes, o
portal de revisão humana) está fora daqui; este módulo só decide e entrega, em dado portátil, o que cada destino precisa.

Classes e ordem (fixa no código, derivada da árvore já validada no projeto de referência; a política não a reordena):

    HUMAN_DECISION          conflito não resolvido, política dura, preocupação específica, alto impacto não verificado,
                            releitura que já foi feita e não resolveu um ponto crítico
    SOURCE_GAP (bloqueante) falta evidência em ponto de criticidade `blocking_from` ou acima
    AI_RECHECK              dúvida de LEITURA sobre evidência que existe, ainda não relida
    SOURCE_GAP              falta evidência em ponto não bloqueante
    SAMPLE_QA               dúvida fora do que bloqueia: entra em amostragem de qualidade
    AUTO_PASS               nenhum sinal pede outra rota

O que a política decide é qual AÇÃO cada sinal pede por criticidade efetiva. A criticidade efetiva é a maior entre a declarada e o
piso da classe de impacto (`impact`), que sai dos efeitos declarados: assim uma afirmação de alto impacto pesa mais sem que alguém a
marque à mão. A confiança é registrada e NUNCA decide: baixa confiança não é revisão humana e alta confiança não suprime um
conflito.

Invariantes que o roteador garante, qualquer que seja a política:

1. evidência existe e a leitura é incerta -> candidata a `AI_RECHECK`; evidência não existe -> `SOURCE_GAP` (um pedido de releitura
   sem evidência vira lacuna de fonte); evidência existe e se contradiz sem a IA poder decidir -> `HUMAN_DECISION`. `SOURCE_GAP`
   nunca vira `HUMAN_DECISION` por si;
2. a classe nunca promove conhecimento. `AUTO_PASS` não é `VALIDATED`: toda decisão grava `validation_effect = NONE`;
3. a releitura por IA é limitada: uma releitura concluída (por alvo e evidência) não é repetida além de `recheck.max_rounds`;
   `INCONCLUSIVE` escala pela política e nunca volta a `AI_RECHECK` por laço. Falha técnica (`FAILED`) não é veredito, não gasta o
   orçamento epistêmico e não gera decisão nova;
4. a amostragem é determinística: o sorteio vem do hash da identidade da decisão e da semente da política, sem acaso e sem relógio;
5. a identidade da decisão inclui a referência da política (id, versão, digest): outra política, outra decisão.

`reproduces` e `is_current` permitem conferir uma decisão guardada: se ela ainda sai dos mesmos sinais e da mesma política, e se a
política mudou desde então.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .analysis_signal import (
    CRITICALITIES,
    DECISION_KINDS,
    REVIEW_CLASSES,
    AnalysisSignal,
    ClaimAssessment,
    EvidenceBinding,
    Position,
    ReviewContractError,
    bindings_tuple,
    check_impact_class,
    check_source_types,
    criticality_rank,
    max_criticality,
)
from .hashing import hash_bytes, hash_canonical_json
from .identity import artifact_identity
from .model_task import (
    BOUNDARIES,
    SCHEMA_URN_PREFIX,
    check_choice,
    check_name,
    check_ref,
    check_text,
    is_int,
    schema_violations,
)
from .paths import find_local_paths
from .review_policy import SAMPLING_CLASSES, ReviewPolicy, default_policy
from .serialization import CanonicalJsonError, canonical_bytes, loads_strict

ENGINE = "review-router/1"
IDENTITY_VERSION = "1"

DECISION_KIND = "review-decision"
DECISION_SCHEMA = f"{SCHEMA_URN_PREFIX}review-decision:1"
DECISION_ARTIFACT = "review.decision"
GAP_KIND = "source-gap-requirement"
GAP_SCHEMA = f"{SCHEMA_URN_PREFIX}source-gap-requirement:1"
GAP_ARTIFACT = "review.source-gap"
CANDIDATE_KIND = "human-decision-candidate"
CANDIDATE_SCHEMA = f"{SCHEMA_URN_PREFIX}human-decision-candidate:1"
CANDIDATE_ARTIFACT = "review.human-candidate"

# Espelha common#/$defs/ladderStep. Um teste confere a igualdade.
LADDER_STEPS = ("prior-validated-knowledge", "evidence-reuse", "documentation", "relations", "ai-recheck", "runtime")
RECHECK_STATUSES = ("RESOLVED", "SOURCE_GAP", "HUMAN_DECISION", "INCONCLUSIVE")
GAP_ORIGINS = ("signal", "ai-recheck")
ROLES = ("primary", "secondary", "addressed")
# Por que a ação final de um sinal é a que é. `rule`: a política mandou. Os demais: ajuste do roteador, sempre explicado.
WHY_CODES = (
    "rule", "no-evidence-to-reread", "recheck-resolved", "recheck-source-gap", "recheck-human-decision",
    "recheck-inconclusive", "recheck-another-round",
)
NEXT_ACTIONS = ("PROCEED", "SAMPLE_REVIEW", "RUN_AI_RECHECK", "ACQUIRE_SOURCE", "REQUEST_HUMAN_DECISION")
_NEXT_ACTION_OF_CLASS = {
    "AUTO_PASS": "PROCEED", "SAMPLE_QA": "PROCEED", "AI_RECHECK": "RUN_AI_RECHECK", "SOURCE_GAP": "ACQUIRE_SOURCE",
    "HUMAN_DECISION": "REQUEST_HUMAN_DECISION",
}
_PURPOSE = {"SAMPLE_QA": "qa", "AUTO_PASS": "audit"}
VALIDATION_EFFECT = "NONE"

_SOFTWARE_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_REASON = re.compile(r"^[A-Z][A-Z_]{1,31}$")
_SIGNAL_ID = re.compile(r"^art:analysis\.signal:sha256:[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^art:ai-recheck\.request:sha256:[0-9a-f]{64}$")
_RESULT_ID = re.compile(r"^art:ai-recheck\.result:sha256:[0-9a-f]{64}$")
_DECISION_ID = re.compile(r"^art:review\.decision:sha256:[0-9a-f]{64}$")
_TEXT_LIMIT = 1000
_MAX_ISSUES = 8

# Texto do que a IA não pode resolver, por tipo de decisão. Frases curtas e genéricas; nenhuma menciona software.
_WHY_AI_CANNOT = {
    "resolve_conflict": "As evidências disponíveis se contradizem e reler a mesma evidência não decide qual posição vale.",
    "validate_claim": "A afirmação é uma inferência de alto impacto; nenhuma análise automática a valida, só uma pessoa nomeada.",
    "apply_policy": "Uma política vigente impede a continuação automática; exceção à política não é decisão da IA.",
    "assess_concern": "A preocupação de privacidade ou segurança é específica deste assunto e exige decisão de pessoa.",
    "resolve_residue": "A releitura por IA da evidência existente já foi feita e não resolveu, e a informação está na evidência.",
}
_RECHECK_DECISION_NOTE = " A releitura por IA concluiu que a evidência sustenta alternativas e não escolhe entre elas."
_QUESTIONS = {
    "resolve_conflict": "Qual posição vale para {target}?",
    "validate_claim": "Confirmar, refinar ou contradizer a afirmação sobre {target}: {summary}",
    "apply_policy": "Como tratar o conflito com a política em {target}: {summary}",
    "assess_concern": "Como tratar a preocupação de privacidade ou segurança em {target}: {summary}",
    "resolve_residue": "A releitura da evidência existente não concluiu sobre {target}. O que vale: {summary}",
}


def _err(code: str, message: str) -> ReviewContractError:
    return ReviewContractError(code, message)


def _clip(text: str, limit: int = _TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _canonical(value: object) -> object:
    try:
        return loads_strict(canonical_bytes(value))
    except CanonicalJsonError as exc:
        raise _err("INVALID_VALUE", f"valor não cabe no JSON canônico: {exc}") from exc


def _record_key(record: dict) -> tuple:
    """Ordem total e estável dos registros de releitura, para que a identidade não dependa da ordem de chegada."""
    return (record["round"], record["request_id"], record["status"], tuple(record["target_refs"]))


def _strictest_boundary(limits: Iterable[str]) -> "str | None":
    found = [check_choice(item, BOUNDARIES, "boundary") for item in limits]
    return min(found, key=BOUNDARIES.index) if found else None


def _identity(artifact: str, inputs: "dict[str, str]", parameters: dict, software_id: "str | None" = None) -> str:
    return artifact_identity(artifact, IDENTITY_VERSION, inputs=inputs, parameters=parameters, software_id=software_id).artifact_id


def _signal_ids(values: object, what: str = "signal_ids") -> "tuple[str, ...]":
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise _err("INVALID_SIGNAL_IDS", f"{what} deve ser uma lista")
    found = tuple(sorted(set(values)))  # type: ignore[arg-type]
    if not found or not all(isinstance(item, str) and _SIGNAL_ID.match(item) for item in found):
        raise _err("INVALID_SIGNAL_IDS", f"{what} deve conter ids de sinal (art:analysis.signal:...)")
    return found


# ----------------------------------------------------------------------------------------------------------------- assunto
@dataclass(frozen=True)
class ReviewSubject:
    """O artefato sob revisão. Qualquer tipo: o roteador não conhece nenhum."""

    ref: str
    kind: str
    title: "str | None" = None
    software_id: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.ref, "ref do assunto")
        check_name(self.kind, "kind do assunto")
        if self.title is not None:
            check_text(self.title, "title do assunto")
        if self.software_id is not None and not (isinstance(self.software_id, str) and _SOFTWARE_ID.match(self.software_id)):
            raise _err("INVALID_SOFTWARE", "software_id inválido")

    def to_dict(self) -> dict:
        data = {"ref": self.ref, "kind": self.kind}
        if self.title is not None:
            data["title"] = self.title
        return data


# ----------------------------------------------------------------------------------------- histórico de releitura por IA
_DETAIL_KEYS = {
    "RESOLVED": (set(), {"confidence"}),
    "SOURCE_GAP": ({"missing", "because"}, {"recommended_source_types"}),
    "HUMAN_DECISION": ({"question", "options"}, set()),
    "INCONCLUSIVE": ({"reason", "unresolved"}, set()),
}


def _check_detail(status: str, detail: object) -> dict:
    if not isinstance(detail, dict):
        raise _err("INVALID_DETAIL", "detail do registro de releitura deve ser objeto")
    detail = _canonical(detail)  # type: ignore[assignment]
    required, optional = _DETAIL_KEYS[status]
    missing, extra = required - set(detail), set(detail) - required - optional  # type: ignore[arg-type]
    if missing or extra:
        raise _err("INVALID_DETAIL", f"detail de {status} malformado (faltam: {sorted(missing)}; sobram: {sorted(extra)})")
    if status == "SOURCE_GAP":
        missing_items = detail["missing"]  # type: ignore[index]
        if not isinstance(missing_items, list) or not missing_items:
            raise _err("INVALID_DETAIL", "missing deve ser lista não vazia")
        for item in missing_items:
            check_text(item, "item de missing")
        check_text(detail["because"], "because")  # type: ignore[index]
        if "recommended_source_types" in detail:  # type: ignore[operator]
            detail["recommended_source_types"] = list(check_source_types(detail["recommended_source_types"]))  # type: ignore[index]
    elif status == "HUMAN_DECISION":
        check_text(detail["question"], "question")  # type: ignore[index]
        options = detail["options"]  # type: ignore[index]
        if not isinstance(options, list) or len(options) < 2:
            raise _err("INVALID_DETAIL", "options deve ter ao menos duas alternativas")
        for item in options:
            check_text(item, "opção")
    elif status == "INCONCLUSIVE":
        # O motivo é um código do executor; o roteador confere só o formato, para não duplicar o vocabulário dele.
        reason = detail["reason"]  # type: ignore[index]
        if not (isinstance(reason, str) and _REASON.match(reason)):
            raise _err("INVALID_DETAIL", "reason deve ser um código em maiúsculas")
        unresolved = detail["unresolved"]  # type: ignore[index]
        if not isinstance(unresolved, list) or not unresolved:
            raise _err("INVALID_DETAIL", "unresolved deve ser lista não vazia")
        for item in unresolved:
            check_ref(item, "ref não resolvida")
    elif "confidence" in detail:  # type: ignore[operator]
        value = detail["confidence"]  # type: ignore[index]
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise _err("INVALID_DETAIL", "confidence deve ser número de 0 a 1")
    return detail  # type: ignore[return-value]


@dataclass(frozen=True)
class RecheckRecord:
    """Uma releitura por IA CONCLUÍDA (resolvida, lacuna, decisão humana ou inconclusiva), para limitar o laço epistêmico.

    Falha técnica não vira registro: não é veredito e não gasta o orçamento. O registro vale para um alvo enquanto a evidência
    atual do alvo estiver contida na que foi relida (`covers`); evidência nova ou alterada reabre o direito a uma releitura.
    """

    target_refs: "tuple[str, ...]"
    evidence: "tuple[EvidenceBinding, ...]"
    round: int
    status: str
    request_id: str
    result_id: str
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        refs = tuple(self.target_refs)
        if isinstance(self.target_refs, (str, bytes)) or not refs:
            raise _err("INVALID_TARGETS", "target_refs deve ser lista não vazia")
        object.__setattr__(self, "target_refs", tuple(sorted({check_ref(item, "target_ref") for item in refs})))
        object.__setattr__(self, "evidence", bindings_tuple(self.evidence))
        if not is_int(self.round) or self.round < 1:
            raise _err("INVALID_ROUND", "round deve ser inteiro a partir de 1")
        check_choice(self.status, RECHECK_STATUSES, "status do registro de releitura")
        if not (isinstance(self.request_id, str) and _REQUEST_ID.match(self.request_id)):
            raise _err("INVALID_ID", "request_id inválido")
        if not (isinstance(self.result_id, str) and _RESULT_ID.match(self.result_id)):
            raise _err("INVALID_ID", "result_id inválido")
        object.__setattr__(self, "detail", _check_detail(self.status, self.detail))

    def covers(self, target_ref: str, evidence: "Iterable[EvidenceBinding]") -> bool:
        """Esta releitura vale para o alvo com esta evidência? Só se o alvo foi relido e a evidência atual cabe na relida."""
        return target_ref in self.target_refs and set(evidence) <= set(self.evidence)

    def to_dict(self) -> dict:
        return {
            "target_refs": list(self.target_refs),
            "evidence": [item.to_dict() for item in self.evidence],
            "round": self.round,
            "status": self.status,
            "request_id": self.request_id,
            "result_id": self.result_id,
            "detail": _canonical(self.detail),
        }

    @classmethod
    def from_dict(cls, data: object) -> "RecheckRecord":
        keys = {"target_refs", "evidence", "round", "status", "request_id", "result_id", "detail"}
        if not isinstance(data, dict) or set(data) != keys:
            raise _err("MALFORMED", "registro de releitura malformado")
        return cls(
            tuple(data["target_refs"]), tuple(EvidenceBinding.from_dict(item) for item in data["evidence"]), data["round"],
            data["status"], data["request_id"], data["result_id"], data["detail"],
        )


# --------------------------------------------------------------------------------------- artefatos de saída por rota
@dataclass(frozen=True)
class SourceGapRequirement:
    """O que falta obter para fechar uma dúvida: informação, motivo, tipos genéricos de fonte, impacto e se bloqueia.

    A identidade não depende da decisão que o gerou: o mesmo pedido, vindo de reavaliações diferentes, tem o mesmo id e se
    deduplica no backlog. Não nomeia adaptador, produto ou sistema: só tipos genéricos de fonte.
    """

    subject_ref: str
    subject_kind: str
    target_ref: str
    missing_information: str
    why_needed: str
    recommended_source_types: "tuple[str, ...]"
    impact_class: str
    criticality: str
    blocking: bool
    origin: str
    signal_ids: "tuple[str, ...]"
    evidence_consulted: "tuple[EvidenceBinding, ...]" = ()
    requirement_id: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.subject_ref, "subject_ref")
        check_name(self.subject_kind, "subject_kind")
        check_ref(self.target_ref, "target_ref")
        check_text(self.missing_information, "missing_information")
        check_text(self.why_needed, "why_needed")
        types = check_source_types(self.recommended_source_types)
        if not types:
            raise _err("INVALID_SOURCE_TYPES", "a lacuna precisa de ao menos um tipo de fonte recomendado")
        object.__setattr__(self, "recommended_source_types", types)
        check_impact_class(self.impact_class)
        check_choice(self.criticality, CRITICALITIES, "criticality")
        if type(self.blocking) is not bool:
            raise _err("INVALID_BLOCKING", "blocking deve ser booleano")
        check_choice(self.origin, GAP_ORIGINS, "origin")
        object.__setattr__(self, "signal_ids", _signal_ids(self.signal_ids))
        object.__setattr__(self, "evidence_consulted", bindings_tuple(self.evidence_consulted, "evidence_consulted"))
        if find_local_paths(self._body()):
            raise _err("LOCAL_PATH", "a lacuna não pode carregar caminho local")
        identity = self._identity()
        if self.requirement_id is not None and self.requirement_id != identity:
            raise _err("ID_MISMATCH", "requirement_id não confere com o conteúdo")
        object.__setattr__(self, "requirement_id", identity)

    def _body(self) -> dict:
        return {
            "subject": {"ref": self.subject_ref, "kind": self.subject_kind},
            "target_ref": self.target_ref,
            "missing_information": self.missing_information,
            "why_needed": self.why_needed,
            "recommended_source_types": list(self.recommended_source_types),
            "impact": {"impact_class": self.impact_class, "criticality": self.criticality},
            "blocking": self.blocking,
            "origin": self.origin,
            "signal_ids": list(self.signal_ids),
        }

    def _identity(self) -> str:
        return _identity(GAP_ARTIFACT, {item.ref: item.sha256 for item in self.evidence_consulted}, self._body())

    def to_dict(self) -> dict:
        data = {"kind": GAP_KIND, "requirement_id": self.requirement_id, **self._body()}
        if self.evidence_consulted:
            data["evidence_consulted"] = [item.to_dict() for item in self.evidence_consulted]
        return data

    @classmethod
    def from_dict(cls, data: object) -> "SourceGapRequirement":
        problems = schema_violations(data, GAP_SCHEMA)
        if problems:
            raise _err("SCHEMA", "lacuna de fonte fora do contrato: " + "; ".join(problems[:_MAX_ISSUES]))
        assert isinstance(data, dict)
        return cls(
            subject_ref=data["subject"]["ref"], subject_kind=data["subject"]["kind"], target_ref=data["target_ref"],
            missing_information=data["missing_information"], why_needed=data["why_needed"],
            recommended_source_types=tuple(data["recommended_source_types"]), impact_class=data["impact"]["impact_class"],
            criticality=data["impact"]["criticality"], blocking=data["blocking"], origin=data["origin"],
            signal_ids=tuple(data["signal_ids"]),
            evidence_consulted=tuple(EvidenceBinding.from_dict(item) for item in data.get("evidence_consulted", ())),
            requirement_id=data["requirement_id"],
        )


@dataclass(frozen=True)
class HumanDecisionCandidate:
    """A fronteira com a revisão humana: o que decidir, por que a IA não decide, o que há de evidência e o que NÃO se pode presumir.

    É só o artefato de fronteira. Não pergunta nem responde por ninguém e não existe portal aqui. Tem a mesma independência de
    decisão de origem que `SourceGapRequirement`.
    """

    subject_ref: str
    subject_kind: str
    target_ref: str
    decision_kind: str
    question: str
    why_ai_cannot_resolve: str
    impact_class: str
    criticality: str
    prohibited_assumptions: "tuple[str, ...]"
    signal_ids: "tuple[str, ...]"
    evidence_available: "tuple[EvidenceBinding, ...]" = ()
    conflicting_claims: "tuple[Position, ...]" = ()
    options: "tuple[str, ...]" = ()
    recommended_role: "str | None" = None
    attempted_before: "tuple[tuple[str, str], ...]" = ()
    candidate_id: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.subject_ref, "subject_ref")
        check_name(self.subject_kind, "subject_kind")
        check_ref(self.target_ref, "target_ref")
        check_choice(self.decision_kind, DECISION_KINDS, "decision_kind")
        check_text(self.question, "question")
        check_text(self.why_ai_cannot_resolve, "why_ai_cannot_resolve")
        check_impact_class(self.impact_class)
        check_choice(self.criticality, CRITICALITIES, "criticality")
        prohibited = tuple(self.prohibited_assumptions)
        if not prohibited:
            raise _err("INVALID_PROHIBITIONS", "o candidato precisa listar o que não se pode presumir")
        object.__setattr__(self, "prohibited_assumptions", tuple(check_text(item, "suposição proibida") for item in prohibited))
        object.__setattr__(self, "signal_ids", _signal_ids(self.signal_ids))
        object.__setattr__(self, "evidence_available", bindings_tuple(self.evidence_available, "evidence_available"))
        claims = tuple(self.conflicting_claims)
        if not all(isinstance(item, Position) for item in claims):
            raise _err("INVALID_POSITIONS", "conflicting_claims deve conter Position")
        object.__setattr__(self, "conflicting_claims", tuple(sorted(set(claims), key=lambda item: (item.ref, item.summary, item.origin or ""))))
        options = tuple(self.options)
        if options and len(options) < 2:
            raise _err("INVALID_OPTIONS", "options, quando houver, deve ter ao menos duas alternativas")
        object.__setattr__(self, "options", tuple(check_text(item, "opção") for item in options))
        if self.recommended_role is not None:
            check_name(self.recommended_role, "recommended_role")
        steps = []
        for step, outcome in self.attempted_before:
            steps.append((check_choice(step, LADDER_STEPS, "passo tentado"), check_text(outcome, "resultado do passo")))
        object.__setattr__(self, "attempted_before", tuple(steps))
        if find_local_paths(self._body()):
            raise _err("LOCAL_PATH", "o candidato não pode carregar caminho local")
        identity = self._identity()
        if self.candidate_id is not None and self.candidate_id != identity:
            raise _err("ID_MISMATCH", "candidate_id não confere com o conteúdo")
        object.__setattr__(self, "candidate_id", identity)

    def _body(self) -> dict:
        body: dict = {
            "subject": {"ref": self.subject_ref, "kind": self.subject_kind},
            "target_ref": self.target_ref,
            "decision_kind": self.decision_kind,
            "question": self.question,
            "why_ai_cannot_resolve": self.why_ai_cannot_resolve,
            "impact": {"impact_class": self.impact_class, "criticality": self.criticality},
            "prohibited_assumptions": list(self.prohibited_assumptions),
            "signal_ids": list(self.signal_ids),
        }
        if self.conflicting_claims:
            body["conflicting_claims"] = [item.to_dict() for item in self.conflicting_claims]
        if self.options:
            body["options"] = list(self.options)
        if self.recommended_role is not None:
            body["recommended_role"] = self.recommended_role
        if self.attempted_before:
            body["attempted_before"] = [{"step": step, "outcome": outcome} for step, outcome in self.attempted_before]
        return body

    def _identity(self) -> str:
        return _identity(CANDIDATE_ARTIFACT, {item.ref: item.sha256 for item in self.evidence_available}, self._body())

    def to_dict(self) -> dict:
        data = {"kind": CANDIDATE_KIND, "candidate_id": self.candidate_id, **self._body()}
        if self.evidence_available:
            data["evidence_available"] = [item.to_dict() for item in self.evidence_available]
        return data

    @classmethod
    def from_dict(cls, data: object) -> "HumanDecisionCandidate":
        problems = schema_violations(data, CANDIDATE_SCHEMA)
        if problems:
            raise _err("SCHEMA", "candidato fora do contrato: " + "; ".join(problems[:_MAX_ISSUES]))
        assert isinstance(data, dict)
        return cls(
            subject_ref=data["subject"]["ref"], subject_kind=data["subject"]["kind"], target_ref=data["target_ref"],
            decision_kind=data["decision_kind"], question=data["question"], why_ai_cannot_resolve=data["why_ai_cannot_resolve"],
            impact_class=data["impact"]["impact_class"], criticality=data["impact"]["criticality"],
            prohibited_assumptions=tuple(data["prohibited_assumptions"]), signal_ids=tuple(data["signal_ids"]),
            evidence_available=tuple(EvidenceBinding.from_dict(item) for item in data.get("evidence_available", ())),
            conflicting_claims=tuple(Position.from_dict(item) for item in data.get("conflicting_claims", ())),
            options=tuple(data.get("options", ())), recommended_role=data.get("recommended_role"),
            attempted_before=tuple((item["step"], item["outcome"]) for item in data.get("attempted_before", ())),
            candidate_id=data["candidate_id"],
        )


# ------------------------------------------------------------------------------------------------------ amostragem
def sample_bucket(seed: str, decision_id: str) -> float:
    """Posição determinística, em [0, 1), de uma decisão na amostra. Só do hash da semente e da identidade: sem acaso, sem relógio."""
    digest = hash_bytes(f"{seed}|{decision_id}".encode("utf-8")).digest
    return int(digest[:13], 16) / float(16 ** 13)


def sampling_block(policy: ReviewPolicy, review_class: str, decision_id: str) -> "dict | None":
    """Bloco de amostragem de qualidade da classe, ou `None` se a classe não é amostrada. A classe NUNCA muda por causa do sorteio."""
    rate = policy.sampling_rate(review_class)
    if rate is None:
        return None
    return {
        "purpose": _PURPOSE[review_class],
        "seed": policy.sampling_seed,
        "rate": rate,
        "selected": sample_bucket(policy.sampling_seed, decision_id) < rate,
    }


def next_action_for(review_class: str, sampling: "dict | None") -> str:
    if sampling is not None and sampling["selected"]:
        return "SAMPLE_REVIEW"
    return _NEXT_ACTION_OF_CLASS[check_choice(review_class, REVIEW_CLASSES, "decision_class")]


# ------------------------------------------------------------------------------------------- avaliação de um sinal
def _tier(action: str, blocking: bool) -> "int | None":
    """Posição na ordem fixa de precedência (0 = vence). `None`: só observação, sem efeito de rota."""
    if action == "HUMAN_DECISION":
        return 0
    if action == "SOURCE_GAP":
        return 1 if blocking else 3
    if action == "AI_RECHECK":
        return 2
    if action == "SAMPLE_QA":
        return 4
    return None


_CLASS_OF_TIER = {0: "HUMAN_DECISION", 1: "SOURCE_GAP", 2: "AI_RECHECK", 3: "SOURCE_GAP", 4: "SAMPLE_QA"}


@dataclass
class _Eval:
    signal: AnalysisSignal
    impact_class: str
    effective: str
    rule_key: str
    rule_action: str
    action: str = "OBSERVE"
    why: str = "rule"
    blocking: bool = False
    decision_kind: "str | None" = None
    detail: "dict | None" = None
    next_round: "int | None" = None
    addressed: bool = False
    role: str = "secondary"

    @property
    def tier(self) -> "int | None":
        return _tier(self.action, self.blocking)

    def assessment(self) -> dict:
        return {
            "signal_id": self.signal.signal_id,
            "family": self.signal.family,
            "rule": self.rule_key,
            "impact_class": self.impact_class,
            "declared_criticality": self.signal.criticality,
            "effective_criticality": self.effective,
            "rule_action": self.rule_action,
            "action": self.action,
            "blocking": self.blocking,
            "why": self.why,
            "role": self.role,
        }


def _evaluate(signal: AnalysisSignal, policy: ReviewPolicy, history: "tuple[RecheckRecord, ...]") -> _Eval:
    impact = policy.derive_impact(signal.impact_class, signal.effects)
    effective = max_criticality(signal.criticality, policy.impact_floor(impact))
    rule = policy.rule_for(signal.family, signal.qualifier)
    action = rule.action(effective)
    item = _Eval(signal, impact, effective, rule.key, action, action)
    if action == "HUMAN_DECISION":
        item.decision_kind = rule.decision_kind
    elif action == "AI_RECHECK":
        _apply_recheck_budget(item, policy, history)
    if item.action == "SOURCE_GAP":
        item.blocking = policy.is_blocking(effective)
    return item


def _apply_recheck_budget(item: _Eval, policy: ReviewPolicy, history: "tuple[RecheckRecord, ...]") -> None:
    """AI_RECHECK pede evidência para reler e tem orçamento. Sem evidência é lacuna; com releitura concluída, vale o desfecho dela."""
    signal = item.signal
    if not signal.evidence:
        item.action, item.why = "SOURCE_GAP", "no-evidence-to-reread"
        return
    covering = [record for record in history if record.covers(signal.target_ref, signal.evidence)]
    if not covering:
        item.next_round = 1
        return
    latest = max(covering, key=lambda record: (record.round, record.request_id))
    item.detail = latest.detail
    if latest.status == "RESOLVED":
        item.action, item.why, item.addressed = "OBSERVE", "recheck-resolved", True
    elif latest.status == "SOURCE_GAP":
        item.action, item.why = "SOURCE_GAP", "recheck-source-gap"
    elif latest.status == "HUMAN_DECISION":
        item.action, item.why, item.decision_kind = "HUMAN_DECISION", "recheck-human-decision", "resolve_conflict"
    elif len(covering) < policy.max_rounds:
        item.next_round, item.why = len(covering) + 1, "recheck-another-round"
    else:
        item.action = policy.on_inconclusive(item.effective)
        item.why = "recheck-inconclusive"
        if item.action == "HUMAN_DECISION":
            item.decision_kind = "resolve_residue"


# ------------------------------------------------------------------- artefatos derivados dos sinais avaliados
def _gap_requirement(subject: ReviewSubject, item: _Eval, policy: ReviewPolicy) -> SourceGapRequirement:
    signal = item.signal
    types: "tuple[str, ...]" = ()
    origin = "signal"
    if item.why == "recheck-source-gap" and item.detail is not None:
        missing = _clip("; ".join(item.detail["missing"]))
        why_needed = item.detail["because"]
        types = tuple(item.detail.get("recommended_source_types", ()))
        origin = "ai-recheck"
    elif signal.source_need is not None:
        missing, why_needed, types = signal.source_need.missing_information, signal.source_need.why_needed, signal.source_need.recommended_source_types
    elif item.why == "no-evidence-to-reread":
        missing = _clip(f"Evidência para reler: {signal.summary}")
        why_needed = "Há dúvida sobre este ponto, mas nenhuma evidência foi consultada: sem o que reler, a releitura por IA não se aplica."
    elif item.why == "recheck-inconclusive":
        missing = _clip(f"Informação que decida: {signal.summary}")
        why_needed = "A releitura da evidência existente foi concluída sem resolver; falta fonte que decida."
    else:
        missing = signal.summary
        why_needed = f"A política exige a fonte para um ponto de criticidade efetiva {item.effective}."
    return SourceGapRequirement(
        subject_ref=subject.ref, subject_kind=subject.kind, target_ref=signal.target_ref,  # type: ignore[arg-type]
        missing_information=missing, why_needed=why_needed,
        recommended_source_types=types or policy.default_source_types, impact_class=item.impact_class, criticality=item.effective,
        blocking=item.blocking, origin=origin, signal_ids=(signal.signal_id,),  # type: ignore[arg-type]
        evidence_consulted=signal.evidence,
    )


def _candidates(
    subject: ReviewSubject, items: "list[_Eval]", policy: ReviewPolicy, history: "tuple[RecheckRecord, ...]"
) -> "tuple[HumanDecisionCandidate, ...]":
    groups: "dict[tuple[str, str], list[_Eval]]" = {}
    for item in items:
        groups.setdefault((item.signal.target_ref, item.decision_kind), []).append(item)  # type: ignore[arg-type,index]
    found: list[HumanDecisionCandidate] = []
    for (target, kind), group in sorted(groups.items()):
        group.sort(key=lambda item: (-criticality_rank(item.effective), item.signal.signal_id))
        summaries = sorted({item.signal.summary for item in group})
        recheck_detail = next((item.detail for item in group if item.why == "recheck-human-decision" and item.detail), None)
        question = (
            recheck_detail["question"] if recheck_detail is not None
            else _clip(_QUESTIONS[kind].format(target=target, summary=" | ".join(summaries)))
        )
        why = _WHY_AI_CANNOT[kind] + (_RECHECK_DECISION_NOTE if recheck_detail is not None else "")
        positions = sorted({position for item in group for position in item.signal.positions}, key=lambda p: (p.ref, p.summary, p.origin or ""))
        if recheck_detail is not None:
            options = tuple(recheck_detail["options"])
        else:
            alternatives = sorted({position.summary for position in positions})
            options = tuple(alternatives) if len(alternatives) >= 2 else ()
        impact = max((item.impact_class for item in group), key=policy.impact_rank)
        attempted = tuple(
            ("ai-recheck", f"{record.status} na passada {record.round}")
            for record in sorted(history, key=lambda r: _record_key(r.to_dict()))
            if target in record.target_refs
        )
        evidence = [binding for item in group for binding in item.signal.evidence]
        found.append(HumanDecisionCandidate(
            subject_ref=subject.ref, subject_kind=subject.kind, target_ref=target, decision_kind=kind,
            question=question, why_ai_cannot_resolve=_clip(why), impact_class=impact,
            criticality=max_criticality(*(item.effective for item in group)),
            prohibited_assumptions=policy.prohibited_assumptions(kind),
            signal_ids=tuple(item.signal.signal_id for item in group),  # type: ignore[misc]
            evidence_available=tuple(evidence), conflicting_claims=tuple(positions), options=options,
            recommended_role=next((item.signal.recommended_role for item in group if item.signal.recommended_role), None),
            attempted_before=attempted,
        ))
    return tuple(sorted(found, key=lambda candidate: candidate.candidate_id))  # type: ignore[arg-type,return-value]


def _recheck_plan(subject: ReviewSubject, items: "list[_Eval]", policy: ReviewPolicy, boundary: "str | None") -> dict:
    by_target: "dict[str, list[_Eval]]" = {}
    for item in items:
        by_target.setdefault(item.signal.target_ref, []).append(item)  # type: ignore[arg-type]
    claims = []
    for target in sorted(by_target):
        group = sorted(by_target[target], key=lambda item: (-criticality_rank(item.effective), item.signal.signal_id))
        claims.append({"claim_ref": target, "statement": group[0].signal.summary})
    highest = max_criticality(*(item.effective for item in items))
    ceiling = policy.default_max_boundary if boundary is None else _strictest_boundary((policy.default_max_boundary, boundary))
    summaries = sorted({item.signal.summary for item in items})
    return {
        "subject": subject.to_dict(),
        "round": max(item.next_round or 1 for item in items),
        "claims": claims,
        "evidence": [binding.to_dict() for binding in bindings_tuple([b for item in items for b in item.signal.evidence])],
        "ambiguity": _clip("; ".join(summaries)),
        "reason": _clip(
            f"Roteado por {policy.policy_id}@{policy.version}: dúvida de interpretação sobre evidência existente "
            f"(criticidade efetiva máxima: {highest})."
        ),
        "max_boundary": ceiling,
        "risk_level": policy.risk_level(highest),
    }


# ------------------------------------------------------------------------------------------------------ a decisão
_DECISION_KEYS = {
    "kind", "decision_id", "engine", "subject", "decision_class", "signals", "assessments", "primary_reasons", "secondary_signals",
    "requires_named_human", "next_action", "policy", "evidence_snapshot", "recheck_history", "validation_effect",
}
_DECISION_OPTIONAL = {
    "software_id", "constraints", "recheck_plan", "source_gap_requirements", "human_decision_candidates", "quality_sampling",
}


@dataclass(frozen=True)
class ReviewDecision:
    """A decisão de roteamento, como dado validado e imutável. Construir confere schema, identidade e coerência interna.

    `decision_id` deriva do assunto, da referência da política, dos sinais, do histórico de releitura e da versão do motor:
    mesmos insumos e mesma política, mesmo id; outra política, outro id. A decisão não promove conhecimento: `validation_effect` é
    sempre `NONE`, e `AUTO_PASS` quer dizer "nenhum sinal pede outra rota", não "correto".
    """

    data: dict
    _signals: "tuple[AnalysisSignal, ...]" = field(init=False, repr=False, compare=False, default=())
    _history: "tuple[RecheckRecord, ...]" = field(init=False, repr=False, compare=False, default=())

    def __post_init__(self) -> None:
        if not isinstance(self.data, dict):
            raise _err("INVALID_DECISION", "a decisão deve ser objeto")
        data = _canonical(self.data)
        assert isinstance(data, dict)
        problems = schema_violations(data, DECISION_SCHEMA)
        if problems:
            raise _err("SCHEMA", "decisão fora do contrato: " + "; ".join(problems[:_MAX_ISSUES]))
        signals = tuple(AnalysisSignal.from_dict(item) for item in data["signals"])
        history = tuple(RecheckRecord.from_dict(item) for item in data["recheck_history"])
        self._check(data, signals, history)
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "_signals", signals)
        object.__setattr__(self, "_history", history)

    @staticmethod
    def _expected_id(data: dict, signals: "tuple[AnalysisSignal, ...]") -> str:
        evidence = {binding.ref: binding.sha256 for signal in signals for binding in signal.evidence}
        parameters = {
            "engine": data["engine"],
            "subject": data["subject"],
            "policy": data["policy"],
            "signals": sorted(signal.signal_id for signal in signals),  # type: ignore[type-var]
            "history": sorted(data["recheck_history"], key=_record_key),
        }
        return _identity(DECISION_ARTIFACT, evidence, parameters, data.get("software_id"))

    @classmethod
    def _check(cls, data: dict, signals: "tuple[AnalysisSignal, ...]", history: "tuple[RecheckRecord, ...]") -> None:
        if data["engine"] != ENGINE:
            raise _err("ENGINE", f"decisão de outro motor: {data['engine']!r}")
        if data["decision_id"] != cls._expected_id(data, signals):
            raise _err("ID_MISMATCH", "decision_id não confere com o conteúdo da decisão")
        if len({signal.signal_id for signal in signals}) != len(signals):
            raise _err("DUPLICATE_SIGNAL", "sinal repetido na decisão")
        for signal in signals:
            if (signal.subject_ref, signal.subject_kind) != (data["subject"]["ref"], data["subject"]["kind"]):
                raise _err("SUBJECT_MISMATCH", "um sinal pertence a outro assunto")
        assessments = data["assessments"]
        if sorted(item["signal_id"] for item in assessments) != sorted(signal.signal_id for signal in signals):  # type: ignore[type-var]
            raise _err("ASSESSMENT_MISMATCH", "as avaliações não cobrem exatamente os sinais da decisão")
        review_class = data["decision_class"]
        tiers = [_tier(item["action"], item["blocking"]) for item in assessments if not item["role"] == "addressed"]
        found = [tier for tier in tiers if tier is not None]
        expected = _CLASS_OF_TIER[min(found)] if found else "AUTO_PASS"
        if review_class != expected:
            raise _err("CLASS_MISMATCH", f"a classe {review_class} não decorre das avaliações (esperada {expected})")
        winning = min(found) if found else None
        for item in assessments:
            tier = _tier(item["action"], item["blocking"])
            if item["role"] == "addressed":
                if item["why"] != "recheck-resolved":
                    raise _err("ROLE_MISMATCH", "só uma releitura resolvida marca um sinal como tratado")
            elif (item["role"] == "primary") != (tier is not None and tier == winning):
                raise _err("ROLE_MISMATCH", "papel do sinal incoerente com a classe")
        if sorted(item["signal_id"] for item in data["primary_reasons"]) != sorted(item["signal_id"] for item in assessments if item["role"] == "primary"):  # type: ignore[type-var]
            raise _err("REASON_MISMATCH", "primary_reasons não confere com as avaliações")
        if sorted(item["signal_id"] for item in data["secondary_signals"]) != sorted(item["signal_id"] for item in assessments if item["role"] == "secondary"):  # type: ignore[type-var]
            raise _err("REASON_MISMATCH", "secondary_signals não confere com as avaliações")
        if data["requires_named_human"] != (review_class == "HUMAN_DECISION"):
            raise _err("HUMAN_MISMATCH", "requires_named_human só vale para HUMAN_DECISION")
        if data["validation_effect"] != VALIDATION_EFFECT:
            raise _err("VALIDATION_EFFECT", "a classe de revisão nunca promove conhecimento")
        sampling = data.get("quality_sampling")
        if (sampling is not None) != (review_class in SAMPLING_CLASSES):
            raise _err("SAMPLING_MISMATCH", "amostragem presente só nas classes amostradas")
        if sampling is not None:
            if sampling["purpose"] != _PURPOSE[review_class]:
                raise _err("SAMPLING_MISMATCH", "propósito da amostragem incoerente com a classe")
            if sampling["selected"] != (sample_bucket(sampling["seed"], data["decision_id"]) < sampling["rate"]):
                raise _err("SAMPLING_MISMATCH", "a seleção não confere com o sorteio determinístico")
        if data["next_action"] != next_action_for(review_class, sampling):
            raise _err("NEXT_ACTION", "next_action incoerente com a classe e a amostragem")
        if (data.get("recheck_plan") is not None) != (review_class == "AI_RECHECK"):
            raise _err("PLAN_MISMATCH", "recheck_plan existe exatamente quando a classe é AI_RECHECK")
        if review_class == "HUMAN_DECISION" and not data.get("human_decision_candidates"):
            raise _err("CANDIDATE_MISSING", "HUMAN_DECISION exige ao menos um candidato")
        if review_class != "HUMAN_DECISION" and data.get("human_decision_candidates"):
            raise _err("CANDIDATE_MISMATCH", "candidato humano só existe quando a classe é HUMAN_DECISION")
        gaps = data.get("source_gap_requirements", [])
        if review_class == "SOURCE_GAP" and not gaps:
            raise _err("GAP_MISSING", "SOURCE_GAP exige ao menos um requisito de fonte")
        for raw in gaps:
            SourceGapRequirement.from_dict(raw)
        for raw in data.get("human_decision_candidates", []):
            HumanDecisionCandidate.from_dict(raw)
        snapshot = data["evidence_snapshot"]
        if snapshot["digest"] != str(hash_canonical_json(snapshot["items"])):
            raise _err("SNAPSHOT", "evidence_snapshot.digest não confere com os itens")
        wanted = [binding.to_dict() for binding in bindings_tuple([b for signal in signals for b in signal.evidence])]
        if snapshot["items"] != wanted:
            raise _err("SNAPSHOT", "evidence_snapshot não confere com a evidência dos sinais")

    # -- leitura
    @property
    def decision_id(self) -> str:
        return self.data["decision_id"]

    @property
    def decision_class(self) -> str:
        return self.data["decision_class"]

    @property
    def subject(self) -> ReviewSubject:
        subject = self.data["subject"]
        return ReviewSubject(subject["ref"], subject["kind"], subject.get("title"), self.data.get("software_id"))

    @property
    def signals(self) -> "tuple[AnalysisSignal, ...]":
        return self._signals

    @property
    def history(self) -> "tuple[RecheckRecord, ...]":
        return self._history

    @property
    def requires_named_human(self) -> bool:
        return self.data["requires_named_human"]

    @property
    def next_action(self) -> str:
        return self.data["next_action"]

    @property
    def policy(self) -> dict:
        return dict(self.data["policy"])

    @property
    def recheck_plan(self) -> "dict | None":
        plan = self.data.get("recheck_plan")
        return _canonical(plan) if plan is not None else None  # type: ignore[return-value]

    @property
    def constraints(self) -> dict:
        return dict(self.data.get("constraints", {}))

    @property
    def quality_sampling(self) -> "dict | None":
        block = self.data.get("quality_sampling")
        return dict(block) if block is not None else None

    @property
    def assessments(self) -> "tuple[dict, ...]":
        return tuple(_canonical(item) for item in self.data["assessments"])  # type: ignore[misc]

    @property
    def primary_reasons(self) -> "tuple[dict, ...]":
        return tuple(_canonical(item) for item in self.data["primary_reasons"])  # type: ignore[misc]

    @property
    def secondary_signals(self) -> "tuple[dict, ...]":
        return tuple(_canonical(item) for item in self.data["secondary_signals"])  # type: ignore[misc]

    @property
    def source_gap_requirements(self) -> "tuple[SourceGapRequirement, ...]":
        return tuple(SourceGapRequirement.from_dict(item) for item in self.data.get("source_gap_requirements", ()))

    @property
    def human_decision_candidates(self) -> "tuple[HumanDecisionCandidate, ...]":
        return tuple(HumanDecisionCandidate.from_dict(item) for item in self.data.get("human_decision_candidates", ()))

    def to_dict(self) -> dict:
        return _canonical(self.data)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: object) -> "ReviewDecision":
        if not isinstance(data, dict):
            raise _err("INVALID_DECISION", "a decisão deve ser objeto")
        return cls(data)


# ------------------------------------------------------------------------------------------------------- o roteador
class ReviewRouter:
    """Roteia sinais por uma política. Sem estado: a mesma chamada sempre devolve a mesma decisão."""

    def __init__(self, policy: "ReviewPolicy | None" = None) -> None:
        if policy is not None and not isinstance(policy, ReviewPolicy):
            raise TypeError("policy deve ser ReviewPolicy")
        self.policy = policy if policy is not None else default_policy()

    # -- derivação de sinais de alto impacto
    def claim_signals(self, subject: ReviewSubject, claims: "Iterable[ClaimAssessment]") -> "tuple[AnalysisSignal, ...]":
        """Sinais `UNVERIFIED_HIGH_IMPACT_CLAIM` derivados: classe de impacto alta (pela política) e status ainda não verificado."""
        found = []
        for claim in claims:
            if not isinstance(claim, ClaimAssessment):
                raise TypeError("claims deve conter ClaimAssessment")
            impact = self.policy.derive_impact(claim.impact_class, claim.effects)
            if self.policy.is_high_impact(impact) and self.policy.is_unverified(claim.status):
                found.append(claim.as_signal(subject.ref, subject.kind))
        return tuple(found)

    def route(
        self,
        subject: ReviewSubject,
        signals: "Iterable[AnalysisSignal]" = (),
        *,
        claims: "Iterable[ClaimAssessment]" = (),
        history: "Iterable[RecheckRecord]" = (),
    ) -> ReviewDecision:
        """Decide a rota de `subject` a partir dos sinais (e das afirmações com efeitos), da política e do histórico de releitura."""
        if not isinstance(subject, ReviewSubject):
            raise TypeError("subject deve ser ReviewSubject")
        policy = self.policy
        gathered: "dict[str, AnalysisSignal]" = {}
        for signal in tuple(signals) + self.claim_signals(subject, tuple(claims)):
            if not isinstance(signal, AnalysisSignal):
                raise TypeError("signals deve conter AnalysisSignal")
            if (signal.subject_ref, signal.subject_kind) != (subject.ref, subject.kind):
                raise _err("SUBJECT_MISMATCH", f"o sinal {signal.signal_id} é de outro assunto")
            gathered[signal.signal_id] = signal  # type: ignore[index]
        records = tuple(history)
        if not all(isinstance(record, RecheckRecord) for record in records):
            raise TypeError("history deve conter RecheckRecord")
        records = tuple(sorted(records, key=lambda record: _record_key(record.to_dict())))
        ordered = [gathered[key] for key in sorted(gathered)]
        items = [_evaluate(signal, policy, records) for signal in ordered]

        tiers = [item.tier for item in items if not item.addressed and item.tier is not None]
        winning = min(tiers) if tiers else None
        review_class = _CLASS_OF_TIER[winning] if winning is not None else "AUTO_PASS"
        for item in items:
            item.role = "addressed" if item.addressed else ("primary" if winning is not None and item.tier == winning else "secondary")

        constraint = _strictest_boundary(signal.boundary_limit for signal in ordered if signal.boundary_limit is not None)
        data: dict = {
            "kind": DECISION_KIND,
            "engine": ENGINE,
            "subject": subject.to_dict(),
            "decision_class": review_class,
            "signals": [signal.to_dict() for signal in ordered],
            "assessments": [item.assessment() for item in items],
            "primary_reasons": [
                {
                    "signal_id": item.signal.signal_id, "family": item.signal.family, "target_ref": item.signal.target_ref,
                    "rule": item.rule_key, "action": item.action, "effective_criticality": item.effective, "why": item.why,
                    "summary": item.signal.summary,
                }
                for item in items if item.role == "primary"
            ],
            "secondary_signals": [
                {"signal_id": item.signal.signal_id, "family": item.signal.family, "action": item.action, "effective_criticality": item.effective}
                for item in items if item.role == "secondary"
            ],
            "requires_named_human": review_class == "HUMAN_DECISION",
            "policy": policy.ref(),
            "recheck_history": [record.to_dict() for record in records],
            "validation_effect": VALIDATION_EFFECT,
        }
        if subject.software_id is not None:
            data["software_id"] = subject.software_id
        snapshot_items = [binding.to_dict() for binding in bindings_tuple([b for signal in ordered for b in signal.evidence])]
        data["evidence_snapshot"] = {"items": snapshot_items, "digest": str(hash_canonical_json(snapshot_items))}
        if constraint is not None:
            data["constraints"] = {"max_boundary": constraint}

        gaps = sorted({
            requirement.requirement_id: requirement
            for requirement in (_gap_requirement(subject, item, policy) for item in items if item.action == "SOURCE_GAP")
        }.items())
        if gaps:
            data["source_gap_requirements"] = [requirement.to_dict() for _, requirement in gaps]
        human_items = [item for item in items if item.action == "HUMAN_DECISION"]
        if human_items:
            data["human_decision_candidates"] = [candidate.to_dict() for candidate in _candidates(subject, human_items, policy, records)]
        if review_class == "AI_RECHECK":
            data["recheck_plan"] = _recheck_plan(subject, [item for item in items if item.action == "AI_RECHECK"], policy, constraint)

        # A identidade cobre o assunto, a política, os sinais e o histórico; a amostragem e a próxima ação são DERIVADAS dela.
        shell = dict(data)
        shell["decision_id"] = ""
        data["decision_id"] = ReviewDecision._expected_id(shell, tuple(ordered))
        sampling = sampling_block(policy, review_class, data["decision_id"])
        if sampling is not None:
            data["quality_sampling"] = sampling
        data["next_action"] = next_action_for(review_class, sampling)
        return ReviewDecision(data)

    # -- conferência de uma decisão guardada
    def is_current(self, decision: ReviewDecision) -> bool:
        """A decisão foi calculada com esta política (id, versão e digest)? Se não, ela deve ser recomputada."""
        return decision.policy == self.policy.ref()

    def reproduces(self, decision: ReviewDecision) -> bool:
        """Recalcular a partir dos mesmos sinais e histórico, com esta política, dá exatamente a mesma decisão?"""
        if not self.is_current(decision):
            return False
        again = self.route(decision.subject, decision.signals, history=decision.history)
        return again.to_dict() == decision.to_dict()

