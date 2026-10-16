"""Apoio dos testes de roteamento de revisão: dois softwares sintéticos sem relação, fábricas de sinais, variantes de política e um
ciclo completo roteamento -> pedido -> executor -> resultado.

Nada aqui conhece um software real nem um provedor. Software 1: um serviço de retenção de registros (documentação). Software 2, sem
relação com o 1: o esquema de um banco de estoque. O roteador e a política são os mesmos para os dois.
"""
from __future__ import annotations

from typing import Callable, Iterable

from adapters.models.scripted import ScriptedModelAdapter
from core.ai_recheck import AIRecheckRequest, AIRecheckResult, RecheckPolicy
from core.analysis_signal import AnalysisSignal, EvidenceBinding
from core.model_task import EvidenceRef
from core.recheck_engine import MappingEvidenceReader, run_recheck
from core.review_policy import ReviewPolicy, default_policy
from core.review_recheck import build_recheck_request, executor_policy
from core.review_routing import ReviewDecision, ReviewRouter, ReviewSubject
from tests.recheck_helpers import DDL, DDL_REF, DOC, DOC_REF, evidence

SUBJECT_ONE = ReviewSubject("job:nightly-retention", "documentation", "Nightly retention job", "synthetic-one")
SUBJECT_TWO = ReviewSubject("table:stock_item", "database-schema", None, "synthetic-two")


def doc_evidence() -> EvidenceRef:
    return evidence(DOC_REF, DOC)


def ddl_evidence() -> EvidenceRef:
    return evidence(DDL_REF, DDL, "database-schema", locator="input:schema-ddl")


def doc_binding() -> EvidenceBinding:
    return EvidenceBinding.of(doc_evidence())


def ddl_binding() -> EvidenceBinding:
    return EvidenceBinding.of(ddl_evidence())


def signal(family: str, criticality: str = "medium", *, subject: ReviewSubject = SUBJECT_ONE, **fields: object) -> AnalysisSignal:
    """Sinal sintético do assunto dado. O resumo padrão é curto e diferente por família e criticidade."""
    fields.setdefault("summary", f"{family.lower().replace('_', ' ')} em {criticality}")
    return AnalysisSignal(family, subject.ref, subject.kind, criticality=criticality, **fields)  # type: ignore[arg-type]


def recheck_signal(criticality: str = "critical", *, target: str = "c1", subject: ReviewSubject = SUBJECT_ONE, **fields: object) -> AnalysisSignal:
    """Dúvida de leitura sobre evidência que EXISTE: candidata a AI_RECHECK quando a criticidade chega ao limiar."""
    fields.setdefault("evidence", (ddl_binding() if subject is SUBJECT_TWO else doc_binding(),))
    return signal("AMBIGUOUS_INTERPRETATION", criticality, subject=subject, target_ref=target, **fields)


def variant(change: Callable[[dict], None]) -> ReviewPolicy:
    """Política derivada da padrão. `change` altera uma cópia dos dados; a política padrão não é tocada."""
    data = default_policy().to_dict()
    change(data)
    return ReviewPolicy(data)


def route(signals: Iterable[AnalysisSignal], *, subject: ReviewSubject = SUBJECT_ONE, policy: "ReviewPolicy | None" = None, **fields: object) -> ReviewDecision:
    return ReviewRouter(policy).route(subject, list(signals), **fields)  # type: ignore[arg-type]


def catalog_for(subject: ReviewSubject) -> "dict[str, EvidenceRef]":
    item = ddl_evidence() if subject is SUBJECT_TWO else doc_evidence()
    return {item.ref: item}


def reader_for(subject: ReviewSubject) -> MappingEvidenceReader:
    return MappingEvidenceReader({DDL_REF: DDL} if subject is SUBJECT_TWO else {DOC_REF: DOC})


def run_cycle(
    decision: ReviewDecision,
    script: list,
    *,
    subject: ReviewSubject = SUBJECT_ONE,
    policy: "ReviewPolicy | None" = None,
    executor: "RecheckPolicy | None" = None,
    attempt: int = 1,
    **adapter_fields: object,
) -> "tuple[AIRecheckRequest, AIRecheckResult, ScriptedModelAdapter]":
    """A decisão AI_RECHECK vira pedido, o executor da Extração 3 roda com um adaptador sintético, e o resultado volta."""
    request = build_recheck_request(decision, catalog_for(subject))
    adapter = ScriptedModelAdapter(script, **adapter_fields)  # type: ignore[arg-type]
    result = run_recheck(
        request, adapter, reader_for(subject), policy=executor or executor_policy(policy or default_policy()), attempt=attempt
    )
    return request, result, adapter
