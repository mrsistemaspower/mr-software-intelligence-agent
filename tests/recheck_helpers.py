"""Apoio dos testes de AI_RECHECK: pedidos sintéticos, respostas prontas de modelo e dois softwares sintéticos sem relação.

Nada aqui conhece um software real nem um provedor. As respostas são as que um modelo devolveria; quem as fabrica é o
`ScriptedModelAdapter` (ou o `RuleBasedModelAdapter`), nunca rede.
"""
from __future__ import annotations

from core.ai_recheck import AIRecheckRequest, PriorInterpretation, RecheckClaim
from core.model_task import EvidenceRef

# Software sintético 1: documentação de um serviço de retenção de registros.
DOC = b"Retention service manual\nThe nightly job keeps records for 90 days.\nArchive after 400 days.\n"
DOC_REF = "doc-1"
# Software sintético 2 (sem relação com o 1): esquema de um banco de estoque.
DDL = b"CREATE TABLE stock_item (\n  item_code VARCHAR(20) NOT NULL,\n  reorder_level INTEGER\n);\n"
DDL_REF = "ddl-1"
# Bytes que não são UTF-8 válido nem texto.
IMAGE = bytes(range(256)) * 3


def evidence(
    ref: str = DOC_REF, data: bytes = DOC, source_type: str = "documentation", modality: str = "text",
    sensitive: "bool | None" = False, locator: "str | None" = "input:evidence-doc", media_type: "str | None" = None,
) -> EvidenceRef:
    return EvidenceRef.of(ref, data, source_type, modality, contains_sensitive_data=sensitive, media_type=media_type, locator=locator)


def request(**overrides: object) -> AIRecheckRequest:
    """Pedido do software sintético 1: um documento, uma afirmação em dúvida, dado que não sai do ambiente."""
    fields: dict = dict(
        subject_ref="job:nightly-retention",
        subject_kind="documentation",
        subject_title="Nightly retention job",
        ambiguity="O prazo de retenção está ambíguo entre 30 e 90 dias.",
        reason="Confiança baixa na primeira leitura.",
        claims=(RecheckClaim("c1", "Os registros são mantidos por 90 dias.", 0.3),),
        evidence=(evidence(),),
        max_boundary="LOCAL",
        prior_interpretation=PriorInterpretation("Provavelmente 30 dias.", "INFERRED", 0.3),
        software_id="synthetic-one",
    )
    fields.update(overrides)
    return AIRecheckRequest(**fields)  # type: ignore[arg-type]


def two_claim_request(**overrides: object) -> AIRecheckRequest:
    claims = (
        RecheckClaim("c1", "Os registros são mantidos por 90 dias.", 0.3),
        RecheckClaim("c2", "O arquivamento ocorre depois de 400 dias.", 0.4),
    )
    return request(claims=claims, **overrides)


def database_request(**overrides: object) -> AIRecheckRequest:
    """Pedido do software sintético 2: outro assunto, outro tipo de fonte, nenhuma tela."""
    fields: dict = dict(
        subject_ref="table:stock_item",
        subject_kind="database-schema",
        ambiguity="Não está claro se reorder_level aceita nulo.",
        reason="O nível de reposição aparece sem restrição explícita.",
        claims=(RecheckClaim("n1", "A coluna reorder_level aceita valor nulo.", 0.4),),
        evidence=(evidence(DDL_REF, DDL, "database-schema", locator="input:schema-ddl"),),
        max_boundary="LOCAL",
        software_id="synthetic-two",
    )
    fields.update(overrides)
    return AIRecheckRequest(**fields)  # type: ignore[arg-type]


def contents(*items: "tuple[str, bytes]") -> dict:
    return dict(items or ((DOC_REF, DOC),))


def finding(claim_ref: str = "c1", refs: "tuple[str, ...]" = (DOC_REF,), confidence: float = 0.9, **extra: object) -> dict:
    data = {
        "claim_ref": claim_ref,
        "statement": "Resolvido a partir da evidência: " + claim_ref,
        "evidence_refs": list(refs),
        "confidence": confidence,
        "basis": "Frase explícita no documento.",
    }
    data.update(extra)
    return data


def resolved(claims: "tuple[str, ...]" = ("c1",), refs: "tuple[str, ...]" = (DOC_REF,), confidence: float = 0.9) -> dict:
    return {
        "status": "RESOLVED",
        "rationale": "O texto da evidência responde às afirmações.",
        "evidence_used": list(refs),
        "findings": [finding(claim, refs, confidence) for claim in claims],
    }


def source_gap(refs: "tuple[str, ...]" = (DOC_REF,), recommended: "str | None" = "runtime") -> dict:
    gap: dict = {"missing": ["O valor configurado em produção."], "insufficient_because": "O documento só descreve o padrão."}
    if recommended is not None:
        gap["recommended_source"] = recommended
    return {"status": "SOURCE_GAP", "rationale": "A evidência não contém o valor efetivo.", "evidence_used": list(refs), "source_gap": gap}


def human_decision(refs: "tuple[str, ...]" = (DOC_REF,)) -> dict:
    return {
        "status": "HUMAN_DECISION",
        "rationale": "A evidência sustenta duas leituras e não escolhe entre elas.",
        "evidence_used": list(refs),
        "decision": {"question": "Qual prazo vale: 30 ou 90 dias?", "options": ["30 dias", "90 dias"]},
    }


def inconclusive(
    unresolved: "tuple[str, ...]" = ("c1",), refs: "tuple[str, ...]" = (DOC_REF,), findings: "list | None" = None
) -> dict:
    data: dict = {
        "status": "INCONCLUSIVE",
        "rationale": "Não foi possível concluir com segurança.",
        "evidence_used": list(refs),
        "unresolved": list(unresolved),
    }
    if findings:
        data["findings"] = findings
    return data
