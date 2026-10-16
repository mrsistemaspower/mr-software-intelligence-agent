"""Ponte entre o roteamento de revisão e o executor de AI_RECHECK. Pura: não executa modelo, não lê arquivo e não chama rede.

O fluxo é este, e este módulo é só as duas setas do meio:

    sinais -> ReviewRouter -> ReviewDecision(AI_RECHECK) --[build_recheck_request]--> AIRecheckRequest
                                                                                          |
                                                              executor (core.recheck_engine / core.recheck_stage)
                                                                                          |
    nova ReviewDecision <--[follow_up]-- AIRecheckResult <--------------------------------+

Ida (`build_recheck_request`): a decisão carrega um `recheck_plan` (assunto, afirmações em dúvida, evidência por referência e hash,
rodada, fronteira de dados, risco). A evidência autorizada de verdade (`EvidenceRef`: tipo, modalidade, sensibilidade, onde ler) vem
de um catálogo que quem chama fornece; o hash de cada item precisa conferir com o do plano, senão a evidência mudou desde o
roteamento e o pedido é recusado. A fronteira só pode APERTAR: o teto do ambiente nunca é afrouxado por este módulo.

Volta (`follow_up`): o resultado é conferido contra o plano (não se aplica resultado de outro pedido) e vira registro de releitura.
Cada estado tem um desfecho diferente, definido pela política do roteador e não por `if` aqui:

- `RESOLVED`: os sinais que pediram a releitura ficam TRATADOS e a decisão é reavaliada (o que sobrar roteia normalmente);
- `SOURCE_GAP`: o ponto vira lacuna de fonte, com o que falta e o tipo de fonte que o modelo indicou;
- `HUMAN_DECISION`: o ponto vira candidato a decisão humana, com a pergunta e as alternativas;
- `INCONCLUSIVE`: com rodadas sobrando, nova rodada; sem rodadas, a escalada que a política define (nunca `AI_RECHECK` em laço).
  Um `INCONCLUSIVE` que resolveu parte das afirmações trata a parte resolvida e escala só o resto;
- `FAILED`: falha TÉCNICA. Não é veredito, não gasta o orçamento de releitura, não gera decisão nova. Quem repete é o motor de
  estágios, e só se `retryable` for verdadeiro.

Nada aqui cria validação humana: o teto epistêmico do executor (`INFERRED`) continua valendo e a classe de revisão não promove nada.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .ai_recheck import AIRecheckRequest, AIRecheckResult, PriorInterpretation, RecheckClaim, RecheckPolicy
from .analysis_signal import EvidenceBinding, ReviewContractError, source_type_for_kind
from .model_task import BOUNDARIES, EvidenceRef, check_choice
from .review_policy import ReviewPolicy
from .review_routing import RecheckRecord, ReviewDecision, ReviewRouter

FOLLOW_UP_REEVALUATED = "REEVALUATED"
FOLLOW_UP_TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
FOLLOW_UP_KINDS = (FOLLOW_UP_REEVALUATED, FOLLOW_UP_TECHNICAL_FAILURE)


def _err(code: str, message: str) -> ReviewContractError:
    return ReviewContractError(code, message)


def executor_policy(policy: ReviewPolicy, **overrides: object) -> RecheckPolicy:
    """Política do executor coerente com a do roteamento: mesmas rodadas e mesmo teto de fronteira.

    Se o roteamento admite N rodadas e o executor admite menos, a rodada extra falha com `ROUND_LIMIT` (não repetível); usar este
    ajuste evita a divergência. `overrides` (por exemplo `min_resolved_confidence`) vão direto para `RecheckPolicy`.
    """
    fields: dict = {"max_rounds": policy.max_rounds, "boundary_ceiling": policy.default_max_boundary}
    fields.update(overrides)
    return RecheckPolicy(**fields)


def _catalog(evidence_catalog: "Mapping[str, EvidenceRef] | Iterable[EvidenceRef]") -> "dict[str, EvidenceRef]":
    items = evidence_catalog.values() if isinstance(evidence_catalog, Mapping) else evidence_catalog
    found: dict[str, EvidenceRef] = {}
    for item in items:
        if not isinstance(item, EvidenceRef):
            raise _err("INVALID_CATALOG", "o catálogo de evidência deve conter EvidenceRef")
        found[item.ref] = item
    return found


def build_recheck_request(
    decision: ReviewDecision,
    evidence_catalog: "Mapping[str, EvidenceRef] | Iterable[EvidenceRef]",
    *,
    prior_interpretation: "PriorInterpretation | None" = None,
    required_capabilities: "Iterable[str]" = (),
    boundary_ceiling: "str | None" = None,
) -> AIRecheckRequest:
    """O `AIRecheckRequest` de uma decisão `AI_RECHECK`. Não executa nada: o executor da Extração 3 recebe o pedido."""
    if not isinstance(decision, ReviewDecision):
        raise TypeError("decision deve ser ReviewDecision")
    plan = decision.recheck_plan
    if decision.decision_class != "AI_RECHECK" or plan is None:
        raise _err("NOT_A_RECHECK", f"a decisão é {decision.decision_class}: só AI_RECHECK gera pedido de releitura")
    catalog = _catalog(evidence_catalog)
    authorized: list[EvidenceRef] = []
    for binding in plan["evidence"]:
        item = catalog.get(binding["ref"])
        if item is None:
            raise _err("EVIDENCE_NOT_AUTHORIZED", f"a evidência {binding['ref']} não está no catálogo autorizado")
        if item.sha256 != binding["sha256"]:
            raise _err("EVIDENCE_CHANGED", f"a evidência {binding['ref']} mudou desde o roteamento: o pedido não é montado")
        authorized.append(item)
    boundary = plan["max_boundary"]
    if boundary_ceiling is not None:
        check_choice(boundary_ceiling, BOUNDARIES, "boundary_ceiling")
        boundary = min(boundary, boundary_ceiling, key=BOUNDARIES.index)  # só aperta, nunca afrouxa
    subject = plan["subject"]
    return AIRecheckRequest(
        subject_ref=subject["ref"],
        subject_kind=subject["kind"],
        subject_title=subject.get("title"),
        ambiguity=plan["ambiguity"],
        reason=plan["reason"],
        claims=tuple(RecheckClaim(item["claim_ref"], item["statement"]) for item in plan["claims"]),
        evidence=tuple(authorized),
        max_boundary=boundary,
        prior_interpretation=prior_interpretation,
        required_capabilities=tuple(required_capabilities),
        risk_level=plan["risk_level"],
        round=plan["round"],
        software_id=decision.subject.software_id,
    )


def plan_problems(decision: ReviewDecision, request: AIRecheckRequest) -> "list[str]":
    """Por que `request` não é o pedido que o plano de `decision` descreve. Lista vazia = é."""
    plan = decision.recheck_plan
    if plan is None:
        return [f"a decisão é {decision.decision_class}, não AI_RECHECK"]
    found: list[str] = []
    if (request.subject_ref, request.subject_kind) != (plan["subject"]["ref"], plan["subject"]["kind"]):
        found.append("assunto diferente")
    if sorted(item.claim_ref for item in request.claims) != sorted(item["claim_ref"] for item in plan["claims"]):
        found.append("afirmações diferentes")
    if [EvidenceBinding.of(item).to_dict() for item in request.evidence] != plan["evidence"]:
        found.append("evidência diferente (ref ou hash)")
    if request.round != plan["round"]:
        found.append("rodada diferente")
    return found


def records_from_result(request: AIRecheckRequest, result: AIRecheckResult) -> "tuple[RecheckRecord, ...]":
    """Registros de releitura concluída de um resultado. Falha técnica (`FAILED`) não gera registro: não é veredito."""
    if not isinstance(request, AIRecheckRequest) or not isinstance(result, AIRecheckResult):
        raise TypeError("request deve ser AIRecheckRequest e result, AIRecheckResult")
    if result.request_id != request.request_id:
        raise _err("RESULT_MISMATCH", "o resultado é de outro pedido de AI_RECHECK")
    status = result.status
    if status == "FAILED":
        return ()
    claims = tuple(item.claim_ref for item in request.claims)
    evidence = tuple(EvidenceBinding.of(item) for item in request.evidence)
    data = result.data

    def record(targets: "tuple[str, ...]", record_status: str, detail: dict) -> RecheckRecord:
        return RecheckRecord(targets, evidence, request.round, record_status, result.request_id, result.result_id, detail)

    if status == "RESOLVED":
        return (record(claims, "RESOLVED", {"confidence": data["confidence"]}),)
    if status == "SOURCE_GAP":
        gap = data["source_gap"]
        detail: dict = {"missing": list(gap["missing"]), "because": gap["insufficient_because"]}
        if gap.get("recommended_source") is not None:
            detail["recommended_source_types"] = [source_type_for_kind(gap["recommended_source"])]
        return (record(claims, "SOURCE_GAP", detail),)
    if status == "HUMAN_DECISION":
        decision = data["decision"]
        return (record(claims, "HUMAN_DECISION", {"question": decision["question"], "options": list(decision["options"])}),)
    unresolved = tuple(sorted(data["inconclusive"]["unresolved"]))
    found = [record(unresolved, "INCONCLUSIVE", {"reason": data["inconclusive"]["reason"], "unresolved": list(unresolved)})]
    settled = tuple(ref for ref in claims if ref not in unresolved)
    if settled:  # o que o modelo resolveu dentro de um INCONCLUSIVE é tratado; só o resto escala
        confidences = [item["confidence"] for item in data.get("findings", ()) if item["claim_ref"] in settled]
        found.append(record(settled, "RESOLVED", {"confidence": min(confidences)} if confidences else {}))
    return tuple(found)


@dataclass(frozen=True)
class ReviewFollowUp:
    """O que acontece depois de um AI_RECHECK, do ponto de vista do roteamento.

    `REEVALUATED`: há uma `decision` nova (e os `records` aplicados). `TECHNICAL_FAILURE`: nenhuma decisão nova; a decisão que
    pediu o recheck continua valendo, o orçamento de releitura não foi gasto e `retryable` diz se o motor de estágios pode repetir.
    """

    kind: str
    decision: "ReviewDecision | None" = None
    records: "tuple[RecheckRecord, ...]" = ()
    failure: "dict | None" = None

    def __post_init__(self) -> None:
        check_choice(self.kind, FOLLOW_UP_KINDS, "kind")
        if (self.kind == FOLLOW_UP_REEVALUATED) != (self.decision is not None):
            raise _err("INVALID_FOLLOW_UP", "REEVALUATED exige decisão nova; TECHNICAL_FAILURE não a tem")
        if (self.kind == FOLLOW_UP_TECHNICAL_FAILURE) != (self.failure is not None):
            raise _err("INVALID_FOLLOW_UP", "TECHNICAL_FAILURE exige a falha; REEVALUATED não a tem")

    @property
    def retryable(self) -> "bool | None":
        """Uma nova tentativa técnica poderia dar certo? `None` quando não houve falha."""
        return bool(self.failure["retryable"]) if self.failure is not None else None

    @property
    def epistemic_budget_consumed(self) -> bool:
        """Uma releitura foi de fato concluída (e gasta uma rodada)? Falha técnica nunca gasta."""
        return bool(self.records)


def follow_up(router: ReviewRouter, decision: ReviewDecision, request: AIRecheckRequest, result: AIRecheckResult) -> ReviewFollowUp:
    """Aplica o resultado de um AI_RECHECK à decisão que o pediu. Pura e determinística."""
    if not isinstance(router, ReviewRouter) or not isinstance(decision, ReviewDecision):
        raise TypeError("router deve ser ReviewRouter e decision, ReviewDecision")
    problems = plan_problems(decision, request)
    if problems:
        raise _err("REQUEST_MISMATCH", "o pedido não é o do plano da decisão: " + "; ".join(problems))
    records = records_from_result(request, result)
    if not records:
        failure = result.failure
        assert failure is not None
        return ReviewFollowUp(FOLLOW_UP_TECHNICAL_FAILURE, None, (), dict(failure))
    again = router.route(decision.subject, decision.signals, history=decision.history + records)
    return ReviewFollowUp(FOLLOW_UP_REEVALUATED, again, records, None)
