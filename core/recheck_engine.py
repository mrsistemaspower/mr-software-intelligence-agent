"""Executor de AI_RECHECK: pedido + adaptador + leitor de evidência -> resultado aceito (ou falha). Sem orquestração, sem provedor.

`run_recheck` é a função pura de verdade do recheck (a única parte que fala com um adaptador). Ela decide, nesta ordem:

1. limite de passadas: um pedido com `round` acima de `max_rounds` da política NÃO é executado (`ROUND_LIMIT`);
2. elegibilidade do adaptador, ANTES de ler qualquer evidência e ANTES de chamá-lo: capacidades exigidas (a modalidade imagem
   exige `vision`), fronteira de dados (o menor entre o pedido e o teto do ambiente) e evidência sensível ou não conferida
   nunca vai a um adaptador externo. Adaptador inelegível não é chamado (`ADAPTER_INELIGIBLE`);
3. integridade da evidência: o conteúdo ATUAL de cada evidência autorizada é lido e conferido contra o hash do pedido.
   Diferente: `EVIDENCE_CHANGED`; ilegível: `EVIDENCE_UNAVAILABLE`; texto que não é UTF-8: `EVIDENCE_UNDECODABLE`;
4. a `ModelTask` (conteúdo conferido de novo contra o hash dentro da própria tarefa);
5. a chamada ao adaptador. Exceção do adaptador é capturada (`ADAPTER_EXCEPTION`); `BaseException` (interrupção do processo) não;
6. a resposta é validada, nunca confiada: resposta de outra tarefa, que não é `ModelResult`, falha técnica do adaptador
   (`ADAPTER_FAILURE`), fora do contrato (`MALFORMED_RESPONSE`) ou com referência de evidência não autorizada
   (`UNAUTHORIZED_EVIDENCE`) viram `FAILED`;
7. um `RESOLVED` abaixo da confiança mínima da política vira `INCONCLUSIVE`;
8. rota, proveniência (adaptador, modelo, hash da resposta, uso, snapshot e verificação da evidência) e `result_id`.

`FAILED` nunca é conhecimento: carrega só o tipo, uma mensagem curta e se uma nova tentativa poderia dar certo. A repetição técnica
é do motor de estágios (`RetryPolicy`), nunca um laço aqui; uma falha NÃO é um `INCONCLUSIVE` e não pede outro recheck.

O executor não grava arquivo, não lê relógio nem ambiente e não conhece SDK. Um adaptador determinístico produz resultado
reprodutível byte a byte; a proveniência só afirma isso quando o adaptador se declara determinístico.
"""
from __future__ import annotations

from typing import Mapping, Protocol

from .ai_recheck import (
    ADAPTER_EXCEPTION,
    ADAPTER_FAILURE,
    ADAPTER_INELIGIBLE,
    EPISTEMIC_CEILING,
    EVIDENCE_CHANGED,
    EVIDENCE_UNAVAILABLE,
    EVIDENCE_UNDECODABLE,
    EXECUTOR,
    FAILED,
    INCONCLUSIVE,
    INSTRUCTIONS,
    MALFORMED_RESPONSE,
    NOT_REPRODUCIBLE,
    OBJECTIVE,
    OUTPUT_CONTRACT,
    REASON_INCONCLUSIVE_LOW_CONFIDENCE,
    REASON_INCONCLUSIVE_MODEL,
    REPRODUCIBLE,
    RESOLVED,
    ROUND_LIMIT,
    SOURCE_GAP,
    HUMAN_DECISION,
    TASK_TYPE,
    AIRecheckRequest,
    AIRecheckResult,
    RecheckPolicy,
    assemble_result,
    route_for,
    validate_model_output,
)
from .hashing import hash_bytes, hash_canonical_json
from .model_adapter import AdapterDescriptor, ModelAdapter, ModelIdentity, ModelResult, check_eligibility
from .model_task import EVIDENCE_KEY, EvidenceRef, ModelContractError, ModelTask, evidence_payload, is_int
from .stage_state import sanitize_message

_REF_LIST_LIMIT = 5


class EvidenceUnavailable(Exception):
    """A evidência autorizada não pôde ser lida agora."""


class EvidenceChanged(Exception):
    """A evidência autorizada mudou depois de autorizada (por exemplo, mudou durante a execução do estágio)."""


class EvidenceReader(Protocol):
    """Como o executor lê o conteúdo ATUAL de uma evidência autorizada. Quem executa decide de onde (arquivo, estágio, memória)."""

    def read(self, evidence: EvidenceRef) -> bytes: ...


class MappingEvidenceReader:
    """Leitor em memória (ref -> bytes). Útil para uso direto do executor, sem orquestração, e para testes."""

    def __init__(self, contents: Mapping[str, bytes]) -> None:
        self._contents = dict(contents)

    def read(self, evidence: EvidenceRef) -> bytes:
        try:
            return self._contents[evidence.ref]
        except KeyError:
            raise EvidenceUnavailable(f"evidência indisponível: {evidence.ref}") from None


def _refs(refs: "list[str]") -> str:
    return ", ".join(refs[:_REF_LIST_LIMIT]) + (" ..." if len(refs) > _REF_LIST_LIMIT else "")


def _provenance(
    request: AIRecheckRequest,
    descriptor: AdapterDescriptor,
    attempt: int,
    *,
    verified: bool,
    task_id: "str | None" = None,
    model: "ModelIdentity | None" = None,
    response_sha256: "str | None" = None,
    usage: "dict | None" = None,
) -> dict:
    snapshot = request.evidence_snapshot()
    data: dict = {
        "executor": EXECUTOR,
        "adapter": descriptor.identity_dict(),
        "evidence_snapshot": snapshot,
        "evidence_snapshot_digest": str(hash_canonical_json(snapshot)),
        "evidence_verified": verified,
        "attempt": attempt,
        "round": request.round,
        "reproducibility": REPRODUCIBLE if descriptor.deterministic else NOT_REPRODUCIBLE,
        "epistemic_ceiling": EPISTEMIC_CEILING,
    }
    if task_id is not None:
        data["task_id"] = task_id
    if model is not None:
        data["model"] = model.to_dict()
    if response_sha256 is not None:
        data["response_sha256"] = response_sha256
    if usage is not None:
        data["usage"] = dict(usage)
    return data


def _failed(
    request: AIRecheckRequest,
    policy: RecheckPolicy,
    descriptor: AdapterDescriptor,
    attempt: int,
    kind: str,
    message: str,
    retryable: bool,
    *,
    reasons: "list[str] | None" = None,
    **provenance: object,
) -> AIRecheckResult:
    failure: dict = {"kind": kind, "message": sanitize_message(message), "retryable": retryable}
    if reasons:
        failure["reasons"] = [sanitize_message(item)[:200] for item in reasons]
    routing = route_for(FAILED, request, policy, failure_kind=kind)
    return assemble_result(
        request.request_id, FAILED, {"failure": failure}, routing,  # type: ignore[arg-type]
        _provenance(request, descriptor, attempt, **provenance),  # type: ignore[arg-type]
    )


def _read_and_verify(request: AIRecheckRequest, reader: EvidenceReader) -> "tuple[dict, list[str], list[str]]":
    """Lê o conteúdo atual e confere cada hash. Devolve (conteúdo por ref, refs alteradas, refs indisponíveis)."""
    contents: dict[str, bytes] = {}
    changed: list[str] = []
    unavailable: list[str] = []
    for item in request.evidence:
        try:
            data = reader.read(item)
        except EvidenceChanged:
            changed.append(item.ref)
            continue
        except Exception:  # noqa: BLE001 - qualquer falha do leitor é "indisponível"; BaseException propaga
            unavailable.append(item.ref)
            continue
        if not isinstance(data, (bytes, bytearray)):
            unavailable.append(item.ref)
            continue
        raw = bytes(data)
        digest = hash_bytes(raw)
        if digest.digest != item.sha256 or digest.byte_length != item.byte_length:
            changed.append(item.ref)
            continue
        contents[item.ref] = raw
    return contents, changed, unavailable


def _context(request: AIRecheckRequest, payloads: dict) -> dict:
    subject: dict = {"ref": request.subject_ref, "kind": request.subject_kind}
    if request.subject_title is not None:
        subject["title"] = request.subject_title
    context: dict = {
        "subject": subject,
        "ambiguity": request.ambiguity,
        "reason": request.reason,
        "claims": [item.to_dict() for item in request.claims],
        EVIDENCE_KEY: payloads,
    }
    if request.prior_interpretation is not None:
        context["prior_interpretation"] = request.prior_interpretation.to_dict()
    return context


def _accepted_body(request: AIRecheckRequest, policy: RecheckPolicy, output: dict) -> "tuple[str, dict]":
    """Estado e corpo ACEITOS a partir de uma resposta já validada: ordem estável e teto epistêmico aplicado."""
    status = output["status"]
    body: dict = {"rationale": output["rationale"], "evidence_used": sorted(output["evidence_used"])}
    findings = sorted(
        (
            {
                "claim_ref": item["claim_ref"],
                "statement": item["statement"],
                "evidence_refs": sorted(item["evidence_refs"]),
                "confidence": item["confidence"],
                "basis": item["basis"],
                "status": EPISTEMIC_CEILING,
            }
            for item in output.get("findings", [])
        ),
        key=lambda item: item["claim_ref"],
    )
    if status == RESOLVED:
        confidence = min(item["confidence"] for item in findings)
        if confidence < policy.min_resolved_confidence:
            every_claim = sorted(item.claim_ref for item in request.claims)
            body["inconclusive"] = {"unresolved": every_claim, "reason": REASON_INCONCLUSIVE_LOW_CONFIDENCE}
            return INCONCLUSIVE, body
        body["findings"] = findings
        body["confidence"] = confidence
    elif status == SOURCE_GAP:
        body["source_gap"] = output["source_gap"]
    elif status == HUMAN_DECISION:
        body["decision"] = output["decision"]
    else:
        if findings:
            body["findings"] = findings
        body["inconclusive"] = {"unresolved": sorted(output["unresolved"]), "reason": REASON_INCONCLUSIVE_MODEL}
    return status, body


def run_recheck(
    request: AIRecheckRequest,
    adapter: ModelAdapter,
    reader: EvidenceReader,
    *,
    policy: "RecheckPolicy | None" = None,
    attempt: int = 1,
) -> AIRecheckResult:
    """Executa um AI_RECHECK. Devolve sempre um `AIRecheckResult` válido; falha técnica é `FAILED`, nunca exceção.

    Levanta exceção só para erro de USO (argumentos de tipo errado, tarefa que o próprio núcleo não consegue montar), nunca para
    o que um adaptador, um leitor ou um modelo fazem.
    """
    if not isinstance(request, AIRecheckRequest):
        raise TypeError("request deve ser AIRecheckRequest")
    policy = policy if policy is not None else RecheckPolicy()
    if not isinstance(policy, RecheckPolicy):
        raise TypeError("policy deve ser RecheckPolicy")
    if not is_int(attempt) or attempt < 1:
        raise ValueError("attempt deve ser inteiro a partir de 1")
    descriptor = adapter.descriptor
    if not isinstance(descriptor, AdapterDescriptor):
        raise TypeError("adapter.descriptor deve ser AdapterDescriptor")

    def failed(kind: str, message: str, retryable: bool, **extra: object) -> AIRecheckResult:
        return _failed(request, policy, descriptor, attempt, kind, message, retryable, **extra)

    if request.round > policy.max_rounds:
        return failed(ROUND_LIMIT, f"o pedido é a passada {request.round}; a política admite {policy.max_rounds}", False, verified=False)

    requirements = request.requirements(policy.boundary_ceiling)
    refusals = check_eligibility(descriptor, requirements)
    if refusals:
        return failed(
            ADAPTER_INELIGIBLE, "adaptador não elegível: " + "; ".join(str(item) for item in refusals), False,
            reasons=[str(item) for item in refusals], verified=False,
        )

    contents, changed, unavailable = _read_and_verify(request, reader)
    if changed:
        return failed(EVIDENCE_CHANGED, "o conteúdo mudou desde que a evidência foi autorizada: " + _refs(changed), False, verified=False)
    if unavailable:
        return failed(EVIDENCE_UNAVAILABLE, "evidência indisponível: " + _refs(unavailable), True, verified=False)

    try:
        payloads = {item.ref: evidence_payload(item, contents[item.ref]) for item in request.evidence}
    except ModelContractError as exc:
        return failed(EVIDENCE_UNDECODABLE, str(exc), False, verified=True)
    task = ModelTask(
        task_type=TASK_TYPE,
        objective=OBJECTIVE,
        instructions=INSTRUCTIONS,
        evidence_refs=request.evidence,
        allowed_context=_context(request, payloads),
        output_contract=OUTPUT_CONTRACT,
        required_capabilities=requirements.required_capabilities,
        max_boundary=requirements.max_boundary,
        risk_level=request.risk_level,
        attempt=attempt,
    )

    try:
        raw = adapter.execute(task)
    except Exception as exc:  # noqa: BLE001 - o adaptador é código de terceiros; BaseException propaga
        detail = str(exc).strip()
        retryable = isinstance(exc, (TimeoutError, ConnectionError))
        return failed(
            ADAPTER_EXCEPTION, type(exc).__name__ + (f": {detail}" if detail else ""), retryable, verified=True, task_id=task.task_id,
        )

    # Resposta repetida a um modelo não determinístico pode sair certa; a de um adaptador determinístico sairá igual.
    retry_malformed = not descriptor.deterministic
    if not isinstance(raw, ModelResult):
        return failed(MALFORMED_RESPONSE, "o adaptador não devolveu um ModelResult", retry_malformed, verified=True, task_id=task.task_id)
    if raw.task_id != task.task_id:
        return failed(MALFORMED_RESPONSE, "a resposta é de outra tarefa", retry_malformed, verified=True, task_id=task.task_id, model=raw.model)
    known = {"task_id": task.task_id, "model": raw.model, "usage": raw.usage, "verified": True}
    if not raw.ok:
        assert raw.failure is not None
        return failed(
            ADAPTER_FAILURE, f"{raw.failure.kind}: {raw.failure.message}", raw.failure.retryable, **known,
        )
    known["response_sha256"] = raw.response_sha256()
    kind, issues = validate_model_output(request, raw.output)
    if kind is not None:
        return failed(kind, "; ".join(issues[:3]), retry_malformed, **known)

    assert raw.output is not None
    status, body = _accepted_body(request, policy, raw.output)
    return assemble_result(
        request.request_id, status, body, route_for(status, request, policy, source_gap=body.get("source_gap")),
        _provenance(request, descriptor, attempt, **known),  # type: ignore[arg-type]
    )
