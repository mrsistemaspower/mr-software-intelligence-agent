"""AI_RECHECK: contratos do pedido, do resultado, da validação da resposta e da rota. Genérico, sem modelo e sem software.

AI_RECHECK é uma SEGUNDA análise focada, por IA, de evidência JÁ disponível, para uma ambiguidade que pode ser resolvida sem
coletar fonte nova, sem perguntar a uma pessoa e sem alterar o sistema de origem. Aqui está a parte que NÃO depende de modelo:

- `AIRecheckRequest`: o assunto (qualquer artefato), a ambiguidade, as afirmações críticas, a evidência AUTORIZADA (por hash), a
  interpretação existente, o motivo e o que se espera. Nada pressupõe tela, botão, aba ou grid.
- `AIRecheckResult`: o resultado ACEITO. Cinco estados: `RESOLVED`, `SOURCE_GAP`, `HUMAN_DECISION`, `INCONCLUSIVE` (o que o modelo
  pode afirmar são os quatro primeiros) e `FAILED` (falha técnica, decidida pelo executor, nunca afirmada pelo modelo).
- `validate_model_output`: a resposta do modelo nunca é confiada. Schema estrito, referências de evidência só as autorizadas,
  cobertura das afirmações, nenhum caminho local. Resposta malformada vira `FAILED`, nunca conhecimento.
- `route_for`: para onde cada estado segue. `RESOLVED` -> reuso; `SOURCE_GAP` -> backlog de aquisição de fonte; `HUMAN_DECISION`
  -> candidato a escalonamento humano; `INCONCLUSIVE` -> rota de contingência da política (por padrão, a escada de
  `core.escalation`); `FAILED` -> tratamento de falha (a repetição técnica é do motor de estágios).

Regras que o contrato impõe:

1. Só evidência autorizada. Uma referência fora da lista invalida a resposta INTEIRA (nada é aceito pela metade).
2. O teto epistêmico é `INFERRED`. Todo achado é `INFERRED`; o modelo nem tem campo para escolher status; um pedido cuja
   interpretação existente já é `VALIDATED` é recusado. Nenhuma segunda passada de IA cria validação humana.
3. Cada pedido é rechecado uma vez (`round`/`max_rounds`). `INCONCLUSIVE` nunca roteia para `AI_RECHECK`: não há laço.
4. Um resultado `RESOLVED` abaixo da confiança mínima da política é rebaixado a `INCONCLUSIVE`: a confiança é um critério do
   executor, não do modelo.
5. A justificativa é curta e auditável. Raciocínio interno não é guardado, nem o conteúdo bruto das evidências.

Este módulo não importa o orquestrador, não executa o modelo (ver `core.recheck_engine`) e não lê relógio, rede ou ambiente.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .epistemics import DEFAULT_LOW_BELOW, check_epistemic
from .escalation import decide
from .hashing import hash_canonical_json
from .identity import artifact_identity
from .model_task import (
    BOUNDARIES,
    RISK_LEVELS,
    SCHEMA_URN_PREFIX,
    EvidenceRef,
    ModelContractError,
    OutputContract,
    TaskRequirements,
    check_choice,
    check_confidence,
    check_name,
    check_output,
    check_ref,
    check_text,
    effective_capabilities,
    is_int,
    schema_violations,
)
from .paths import find_local_paths
from .serialization import CanonicalJsonError, canonical_bytes, loads_strict

REQUEST_KIND = "ai-recheck-request"
RESULT_KIND = "ai-recheck-result"
REQUEST_SCHEMA = f"{SCHEMA_URN_PREFIX}ai-recheck-request:1"
RESULT_SCHEMA = f"{SCHEMA_URN_PREFIX}ai-recheck-result:1"
REQUEST_ARTIFACT = "ai-recheck.request"
RESULT_ARTIFACT = "ai-recheck.result"
IDENTITY_VERSION = "1"
TASK_TYPE = "ai-recheck"
EXECUTOR = "ai-recheck/1"
OUTPUT_CONTRACT = OutputContract("ai-recheck-output", "1", f"{RESULT_SCHEMA}#/$defs/modelOutput")
EXPECTED_OUTPUT = {"contract": OUTPUT_CONTRACT.name, "version": OUTPUT_CONTRACT.version}

RESOLVED = "RESOLVED"
SOURCE_GAP = "SOURCE_GAP"
HUMAN_DECISION = "HUMAN_DECISION"
INCONCLUSIVE = "INCONCLUSIVE"
FAILED = "FAILED"
MODEL_STATUSES = (RESOLVED, SOURCE_GAP, HUMAN_DECISION, INCONCLUSIVE)
STATUSES = MODEL_STATUSES + (FAILED,)

ROUTE_REUSE = "REUSE"
ROUTE_FAILURE = "FAILURE"
ROUTES = (ROUTE_REUSE, "SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION", ROUTE_FAILURE)
# Rotas permitidas para INCONCLUSIVE. AI_RECHECK fica de fora de propósito: repetir a análise não é contingência, é laço.
FALLBACK_ROUTES = ("SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION")
DESTINATION_AI_RESULT = "ai_result"
DESTINATION_SOURCE_BACKLOG = "source_backlog"
DESTINATION_HUMAN_REVIEW = "human_review_candidates"
DESTINATION_FAILURE = "failure_handling"
DESTINATIONS = {
    ROUTE_REUSE: DESTINATION_AI_RESULT,
    "SOURCE_GAP": DESTINATION_SOURCE_BACKLOG,
    "HUMAN_DECISION": DESTINATION_HUMAN_REVIEW,
    "HUMAN_VALIDATION": DESTINATION_HUMAN_REVIEW,
    ROUTE_FAILURE: DESTINATION_FAILURE,
}

# Tipos de falha do executor (fechados). Nenhum deles é conhecimento.
ROUND_LIMIT = "ROUND_LIMIT"
ADAPTER_INELIGIBLE = "ADAPTER_INELIGIBLE"
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
EVIDENCE_CHANGED = "EVIDENCE_CHANGED"
EVIDENCE_UNDECODABLE = "EVIDENCE_UNDECODABLE"
ADAPTER_FAILURE = "ADAPTER_FAILURE"
ADAPTER_EXCEPTION = "ADAPTER_EXCEPTION"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
UNAUTHORIZED_EVIDENCE = "UNAUTHORIZED_EVIDENCE"
FAILURE_KINDS = (
    ROUND_LIMIT, ADAPTER_INELIGIBLE, EVIDENCE_UNAVAILABLE, EVIDENCE_CHANGED, EVIDENCE_UNDECODABLE,
    ADAPTER_FAILURE, ADAPTER_EXCEPTION, MALFORMED_RESPONSE, UNAUTHORIZED_EVIDENCE,
)

REASON_INCONCLUSIVE_MODEL = "MODEL_REPORTED"
REASON_INCONCLUSIVE_LOW_CONFIDENCE = "LOW_CONFIDENCE"
PRIOR_STATUSES = ("OBSERVED", "INFERRED")
EPISTEMIC_CEILING = "INFERRED"
REPRODUCIBLE = "DETERMINISTIC_ADAPTER"
NOT_REPRODUCIBLE = "NOT_GUARANTEED"

# Instruções curtas e genéricas, em dados. Nenhum prompt de provedor mora aqui.
OBJECTIVE = "Resolver as afirmações em dúvida usando somente a evidência autorizada, sem conhecimento externo."
INSTRUCTIONS = (
    "Use somente as evidências em allowed_context.evidence; não use outro conhecimento sobre o software.",
    "Para cada afirmação em allowed_context.claims, decida com base nas evidências e cite as refs usadas.",
    "Se a evidência não contém a informação, responda SOURCE_GAP e diga o que falta; não adivinhe.",
    "Se a evidência sustenta alternativas e a escolha não é decidida por ela, responda HUMAN_DECISION com as opções.",
    "Se não conseguir concluir nem classificar, responda INCONCLUSIVE e liste as afirmações não resolvidas.",
    "Dê só uma justificativa curta e auditável; não inclua raciocínio interno e nunca declare algo validado por pessoa.",
)

_SOFTWARE_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_REASON = re.compile(r"^[a-z][a-z0-9:-]{0,63}$")
_MAX_ISSUES = 8


class RecheckError(ModelContractError):
    """Pedido, política ou resultado de AI_RECHECK fora das regras. `code` é estável."""


def _err(code: str, message: str) -> RecheckError:
    return RecheckError(code, message)


# ---------------------------------------------------------------------------------------------------------------- pedido
@dataclass(frozen=True)
class RecheckClaim:
    """Uma afirmação (ou item crítico) em dúvida. `claim_ref` é o identificador que a resposta deve citar."""

    claim_ref: str
    statement: str
    current_confidence: "float | None" = None

    def __post_init__(self) -> None:
        check_ref(self.claim_ref, "claim_ref")
        check_text(self.statement, "statement da afirmação")
        if self.current_confidence is not None:
            check_confidence(self.current_confidence, "current_confidence")

    def to_dict(self) -> dict:
        data: dict = {"claim_ref": self.claim_ref, "statement": self.statement}
        if self.current_confidence is not None:
            data["current_confidence"] = self.current_confidence
        return data


@dataclass(frozen=True)
class PriorInterpretation:
    """A interpretação existente, em dúvida. Nunca `VALIDATED`: o que uma pessoa validou não é rechecado por IA."""

    summary: str
    status: str
    confidence: "float | None" = None

    def __post_init__(self) -> None:
        check_text(self.summary, "summary da interpretação existente")
        if self.status == "VALIDATED":
            raise _err("VALIDATED_PRIOR", "conhecimento VALIDATED por pessoa não é rechecado por IA")
        check_choice(self.status, PRIOR_STATUSES, "status da interpretação existente")
        if self.confidence is not None:
            check_confidence(self.confidence, "confidence da interpretação existente")

    def to_dict(self) -> dict:
        data: dict = {"summary": self.summary, "status": self.status}
        if self.confidence is not None:
            data["confidence"] = self.confidence
        return data


@dataclass(frozen=True)
class AIRecheckRequest:
    """Pedido de AI_RECHECK. Imutável; `request_id` deriva do conteúdo e é conferido ao construir e ao carregar."""

    subject_ref: str
    subject_kind: str
    ambiguity: str
    reason: str
    claims: "tuple[RecheckClaim, ...]"
    evidence: "tuple[EvidenceRef, ...]"
    max_boundary: str
    subject_title: "str | None" = None
    prior_interpretation: "PriorInterpretation | None" = None
    required_capabilities: "tuple[str, ...]" = ()
    risk_level: str = "NORMAL"
    round: int = 1
    software_id: "str | None" = None
    request_id: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.subject_ref, "subject_ref")
        check_name(self.subject_kind, "subject_kind")
        if self.subject_title is not None:
            check_text(self.subject_title, "subject_title")
        check_text(self.ambiguity, "ambiguity")
        check_text(self.reason, "reason")

        claims = tuple(self.claims)
        if not claims or not all(isinstance(item, RecheckClaim) for item in claims):
            raise _err("INVALID_CLAIMS", "o pedido precisa de ao menos uma afirmação (RecheckClaim)")
        if len({item.claim_ref for item in claims}) != len(claims):
            raise _err("DUPLICATE_CLAIM", "claim_ref repetido")
        object.__setattr__(self, "claims", tuple(sorted(claims, key=lambda item: item.claim_ref)))

        evidence = tuple(self.evidence)
        if not evidence or not all(isinstance(item, EvidenceRef) for item in evidence):
            raise _err("NO_EVIDENCE", "AI_RECHECK sem evidência não é recheck: sem o que reler, a rota é SOURCE_GAP")
        if len({item.ref for item in evidence}) != len(evidence):
            raise _err("DUPLICATE_EVIDENCE", "referência de evidência repetida")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=lambda item: item.ref)))

        check_choice(self.max_boundary, BOUNDARIES, "max_boundary")
        check_choice(self.risk_level, RISK_LEVELS, "risk_level")
        if not is_int(self.round) or self.round < 1:
            raise _err("INVALID_ROUND", "round deve ser inteiro a partir de 1")
        if self.prior_interpretation is not None and not isinstance(self.prior_interpretation, PriorInterpretation):
            raise _err("INVALID_PRIOR", "prior_interpretation deve ser PriorInterpretation")
        object.__setattr__(self, "required_capabilities", effective_capabilities(self.required_capabilities, ()))
        if self.software_id is not None and not (isinstance(self.software_id, str) and _SOFTWARE_ID.match(self.software_id)):
            raise _err("INVALID_SOFTWARE", "software_id inválido")
        if find_local_paths(self._body()):
            raise _err("LOCAL_PATH", "o pedido não pode carregar caminho local")

        identity = self._identity()
        if self.request_id is not None and self.request_id != identity:
            raise _err("ID_MISMATCH", "request_id não confere com o conteúdo do pedido")
        object.__setattr__(self, "request_id", identity)

    def _body(self) -> dict:
        body: dict = {
            "round": self.round,
            "subject": {"ref": self.subject_ref, "kind": self.subject_kind},
            "ambiguity": self.ambiguity,
            "reason": self.reason,
            "claims": [item.to_dict() for item in self.claims],
            "evidence": [item.to_dict() for item in self.evidence],
            "expected_output": dict(EXPECTED_OUTPUT),
            "required_capabilities": list(self.required_capabilities),
            "data_policy": {"max_boundary": self.max_boundary},
            "risk_level": self.risk_level,
        }
        if self.subject_title is not None:
            body["subject"]["title"] = self.subject_title
        if self.prior_interpretation is not None:
            body["prior_interpretation"] = self.prior_interpretation.to_dict()
        return body

    def _identity(self) -> str:
        return artifact_identity(
            REQUEST_ARTIFACT,
            IDENTITY_VERSION,
            inputs={item.ref: item.sha256 for item in self.evidence},
            parameters=self._body(),
            software_id=self.software_id,
        ).artifact_id

    def effective_capabilities(self) -> "tuple[str, ...]":
        """O que o adaptador precisa oferecer: o pedido, `structured-output` e o que a modalidade da evidência impõe."""
        return effective_capabilities(set(self.required_capabilities) | {"structured-output"}, self.evidence)

    def requirements(self, boundary_ceiling: "str | None" = None) -> TaskRequirements:
        """Exigências sobre o adaptador. `boundary_ceiling` (do ambiente) pode apertar, nunca afrouxar, o que o pedido declara."""
        boundary = self.max_boundary
        if boundary_ceiling is not None and BOUNDARIES.index(check_choice(boundary_ceiling, BOUNDARIES, "boundary_ceiling")) < BOUNDARIES.index(boundary):
            boundary = boundary_ceiling
        return TaskRequirements(self.effective_capabilities(), boundary, self.evidence)

    def evidence_snapshot(self) -> "list[dict]":
        """O que o pedido autorizou, por ref: hash e tamanho. É a linha de base contra a qual o conteúdo atual é conferido."""
        return [{"ref": item.ref, "sha256": item.sha256, "byte_length": item.byte_length} for item in self.evidence]

    def to_dict(self) -> dict:
        data = {"kind": REQUEST_KIND, "request_id": self.request_id, **self._body()}
        if self.software_id is not None:
            data["software_id"] = self.software_id
        return data

    @classmethod
    def from_dict(cls, data: object) -> "AIRecheckRequest":
        problems = schema_violations(data, REQUEST_SCHEMA)
        if problems:
            raise _err("SCHEMA", "pedido fora do contrato: " + "; ".join(problems[:_MAX_ISSUES]))
        assert isinstance(data, dict)
        subject = data["subject"]
        prior = data.get("prior_interpretation")
        return cls(
            subject_ref=subject["ref"],
            subject_kind=subject["kind"],
            subject_title=subject.get("title"),
            ambiguity=data["ambiguity"],
            reason=data["reason"],
            claims=tuple(RecheckClaim(**item) for item in data["claims"]),
            evidence=tuple(EvidenceRef.from_dict(item) for item in data["evidence"]),
            max_boundary=data["data_policy"]["max_boundary"],
            prior_interpretation=PriorInterpretation(**prior) if prior is not None else None,
            required_capabilities=tuple(data["required_capabilities"]),
            risk_level=data["risk_level"],
            round=data["round"],
            software_id=data.get("software_id"),
            request_id=data["request_id"],
        )


@dataclass(frozen=True)
class RecheckPolicy:
    """Política do executor. Dado portátil: entra nos parâmetros do estágio, então mudá-la invalida o trabalho feito.

    - `max_rounds`: quantas passadas de recheck um pedido admite (padrão 1: rechecado uma vez, nunca repetido);
    - `min_resolved_confidence`: abaixo disto um `RESOLVED` vira `INCONCLUSIVE`;
    - `inconclusive_route`: rota de contingência de `INCONCLUSIVE`; `None` usa a escada de `core.escalation`;
    - `boundary_ceiling`: até onde o AMBIENTE deixa o dado ir, por cima do que cada pedido declara.
    """

    max_rounds: int = 1
    min_resolved_confidence: float = DEFAULT_LOW_BELOW
    inconclusive_route: "str | None" = None
    boundary_ceiling: str = "EXTERNAL"

    def __post_init__(self) -> None:
        if not is_int(self.max_rounds) or not 1 <= self.max_rounds <= 5:
            raise _err("INVALID_POLICY", "max_rounds deve ser inteiro de 1 a 5")
        check_confidence(self.min_resolved_confidence, "min_resolved_confidence")
        if self.inconclusive_route is not None:
            if self.inconclusive_route == "AI_RECHECK":
                raise _err("RECHECK_LOOP", "INCONCLUSIVE não pode rotear para AI_RECHECK: repetir a análise é laço, não contingência")
            check_choice(self.inconclusive_route, FALLBACK_ROUTES, "inconclusive_route")
        check_choice(self.boundary_ceiling, BOUNDARIES, "boundary_ceiling")

    def to_dict(self) -> dict:
        data: dict = {
            "max_rounds": self.max_rounds,
            "min_resolved_confidence": self.min_resolved_confidence,
            "boundary_ceiling": self.boundary_ceiling,
        }
        if self.inconclusive_route is not None:
            data["inconclusive_route"] = self.inconclusive_route
        return data


# ----------------------------------------------------------------------------------------------- validação da resposta
def validate_model_output(request: AIRecheckRequest, output: object) -> "tuple[str | None, list[str]]":
    """Valida a resposta BRUTA do modelo. Devolve `(None, [])` se aceitável; senão `(tipo de falha, violações)`.

    Ordem: schema estrito; referências de evidência (só as autorizadas, em `evidence_used` e em cada achado); depois a
    coerência com as afirmações do pedido. Determinística e sem efeito colateral.
    """
    problems = check_output(OUTPUT_CONTRACT, output)
    if problems:
        return MALFORMED_RESPONSE, problems
    assert isinstance(output, dict)
    findings = output.get("findings", [])
    allowed = {item.ref for item in request.evidence}
    used = set(output["evidence_used"])
    for finding in findings:
        used |= set(finding["evidence_refs"])
    unknown = sorted(used - allowed)
    if unknown:
        return UNAUTHORIZED_EVIDENCE, [f"referência de evidência não autorizada: {ref}" for ref in unknown[:_MAX_ISSUES]]

    claims = {item.claim_ref for item in request.claims}
    resolved = [finding["claim_ref"] for finding in findings]
    unresolved = output.get("unresolved", [])
    status = output["status"]
    issues: list[str] = []
    if len(set(resolved)) != len(resolved):
        issues.append("mais de um achado para a mesma afirmação")
    if set(resolved) - claims:
        issues.append(f"achado para afirmação que o pedido não tem: {sorted(set(resolved) - claims)[:_MAX_ISSUES]}")
    if status == RESOLVED and set(resolved) != claims:
        issues.append(f"RESOLVED deve tratar todas as afirmações; faltam: {sorted(claims - set(resolved))[:_MAX_ISSUES]}")
    if status == INCONCLUSIVE:
        if set(unresolved) - claims:
            issues.append(f"não resolvida desconhecida: {sorted(set(unresolved) - claims)[:_MAX_ISSUES]}")
        if set(unresolved) & set(resolved):
            issues.append("afirmação resolvida e não resolvida ao mesmo tempo")
        if (set(unresolved) | set(resolved)) != claims:
            issues.append("INCONCLUSIVE deve acomodar todas as afirmações (resolvidas ou listadas como não resolvidas)")
    if find_local_paths(output):
        issues.append("a resposta contém texto que parece caminho local")
    return (MALFORMED_RESPONSE, issues) if issues else (None, [])


# ------------------------------------------------------------------------------------------------------------- rota
def route_for(
    status: str,
    request: AIRecheckRequest,
    policy: RecheckPolicy,
    *,
    source_gap: "dict | None" = None,
    failure_kind: "str | None" = None,
) -> dict:
    """Para onde um resultado de `status` segue. Pura. `proceeds` só é verdadeiro para `REUSE`."""
    check_choice(status, STATUSES, "status")
    if status == RESOLVED:
        return _routing(ROUTE_REUSE, "resolved")
    if status == SOURCE_GAP:
        needed = (source_gap or {}).get("recommended_source")
        return _routing("SOURCE_GAP", "model-reported-source-gap", needed)
    if status == HUMAN_DECISION:
        return _routing("HUMAN_DECISION", "model-reported-human-decision")
    if status == FAILED:
        return _routing(ROUTE_FAILURE, "failure:" + (failure_kind or "unknown").lower().replace("_", "-"))
    if policy.inconclusive_route is not None:
        return _routing(policy.inconclusive_route, "inconclusive-fallback-policy")
    prior = request.prior_interpretation
    decision = decide({
        "status": prior.status if prior is not None else "INFERRED",
        "evidence_present": True,
        "confidence": 0.0,
        "recheck_done": True,
        "recheck_resolved": False,
        "inference_to_confirm": prior is not None,
    })
    return _routing(decision.route, "inconclusive-fallback-ladder", decision.needed_source)


def _routing(route: str, reason: str, needed_source: "str | None" = None) -> dict:
    data: dict = {"route": route, "destination": DESTINATIONS[route], "proceeds": route == ROUTE_REUSE, "reason": reason}
    if needed_source is not None:
        data["needed_source"] = needed_source
    return data


# --------------------------------------------------------------------------------------------------------- resultado
def _result_identity(base: dict) -> str:
    return artifact_identity(
        RESULT_ARTIFACT,
        IDENTITY_VERSION,
        inputs={"body": hash_canonical_json(base)},
        parameters={"request_id": base["request_id"]},
    ).artifact_id


_NON_BODY = ("kind", "result_id", "routing", "provenance")
_ALLOWED_ROUTES = {
    RESOLVED: (ROUTE_REUSE,),
    SOURCE_GAP: ("SOURCE_GAP",),
    HUMAN_DECISION: ("HUMAN_DECISION",),
    INCONCLUSIVE: FALLBACK_ROUTES,
    FAILED: (ROUTE_FAILURE,),
}


@dataclass(frozen=True)
class AIRecheckResult:
    """Resultado aceito de um AI_RECHECK (dado validado e imutável). Construir confere schema e coerência; nada inválido existe.

    `result_id` deriva do corpo aceito (sem rota nem proveniência): quem produziu, quando e com qual modelo é metadado ao lado.
    """

    data: dict

    def __post_init__(self) -> None:
        if not isinstance(self.data, dict):
            raise _err("INVALID_RESULT", "o resultado deve ser objeto")
        try:
            data = loads_strict(canonical_bytes(self.data))
        except CanonicalJsonError as exc:
            raise _err("INVALID_RESULT", f"resultado não cabe no JSON canônico: {exc}") from exc
        assert isinstance(data, dict)
        problems = schema_violations(data, RESULT_SCHEMA)
        if problems:
            raise _err("SCHEMA", "resultado fora do contrato: " + "; ".join(problems[:_MAX_ISSUES]))
        self._check(data)
        object.__setattr__(self, "data", data)

    @staticmethod
    def _check(data: dict) -> None:
        base = {key: value for key, value in data.items() if key not in _NON_BODY}
        if data["result_id"] != _result_identity(base):
            raise _err("ID_MISMATCH", "result_id não confere com o corpo do resultado")
        status, routing, provenance = data["status"], data["routing"], data["provenance"]
        if routing["route"] not in _ALLOWED_ROUTES[status]:
            raise _err("ROUTE_MISMATCH", f"a rota {routing['route']} não cabe no estado {status}")
        if routing["destination"] != DESTINATIONS[routing["route"]] or routing["proceeds"] != (routing["route"] == ROUTE_REUSE):
            raise _err("ROUTE_MISMATCH", "destino ou proceeds incoerentes com a rota")
        if not _REASON.match(routing["reason"]):
            raise _err("ROUTE_MISMATCH", "motivo da rota fora do formato")
        if status == FAILED and routing["reason"].split(":")[0] != "failure":
            raise _err("ROUTE_MISMATCH", "uma falha deve rotear por motivo failure:*")
        for finding in data.get("findings", []):
            epistemic = {
                "status": finding["status"],
                "confidence": finding["confidence"],
                "provenance": [{"evidence_ref": ref} for ref in finding["evidence_refs"]],
                "basis": finding["basis"],
            }
            issues = check_epistemic(epistemic)
            if issues or finding["status"] != EPISTEMIC_CEILING:
                raise _err("EPISTEMIC", "achado fora da disciplina de evidência: " + "; ".join(issues or ["status acima do teto"]))
        if status == RESOLVED and data["confidence"] != min(item["confidence"] for item in data["findings"]):
            raise _err("CONFIDENCE", "a confiança de RESOLVED deve ser a menor entre os achados")
        snapshot = provenance["evidence_snapshot"]
        if provenance["evidence_snapshot_digest"] != str(hash_canonical_json(snapshot)):
            raise _err("SNAPSHOT", "evidence_snapshot_digest não confere com evidence_snapshot")
        if find_local_paths(data):
            raise _err("LOCAL_PATH", "o resultado não pode carregar caminho local")

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def request_id(self) -> str:
        return self.data["request_id"]

    @property
    def result_id(self) -> str:
        return self.data["result_id"]

    @property
    def routing(self) -> dict:
        return dict(self.data["routing"])

    @property
    def route(self) -> str:
        return self.data["routing"]["route"]

    @property
    def proceeds(self) -> bool:
        return self.data["routing"]["proceeds"]

    @property
    def provenance(self) -> dict:
        return loads_strict(canonical_bytes(self.data["provenance"]))  # type: ignore[return-value]

    @property
    def failure(self) -> "dict | None":
        failure = self.data.get("failure")
        return dict(failure) if failure is not None else None

    @property
    def reusable_findings(self) -> "tuple[dict, ...]":
        """Os achados reutilizáveis: só quando a rota é `REUSE`. Em qualquer outro caso, nada é reutilizável."""
        if not self.proceeds:
            return ()
        return tuple(loads_strict(canonical_bytes(item)) for item in self.data["findings"])  # type: ignore[misc]

    def to_dict(self) -> dict:
        return loads_strict(canonical_bytes(self.data))  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: object) -> "AIRecheckResult":
        if not isinstance(data, dict):
            raise _err("INVALID_RESULT", "o resultado deve ser objeto")
        return cls(data)


def assemble_result(request_id: str, status: str, body: dict, routing: dict, provenance: dict) -> AIRecheckResult:
    """Monta um resultado: calcula `result_id` do corpo aceito (`request_id`, `status` e `body`) e valida tudo."""
    base = {"request_id": request_id, "status": status}
    base.update(body)
    data = {"kind": RESULT_KIND, "result_id": _result_identity(base), **base, "routing": routing, "provenance": provenance}
    return AIRecheckResult(data)


# ------------------------------------------------------------------------- visões para quem consome a rota
def _subject(request: AIRecheckRequest) -> dict:
    subject: dict = {"ref": request.subject_ref, "kind": request.subject_kind}
    if request.subject_title is not None:
        subject["title"] = request.subject_title
    return subject


def source_gap_entry(request: AIRecheckRequest, result: AIRecheckResult) -> dict:
    """Entrada do backlog de aquisição de fonte para um resultado roteado a `SOURCE_GAP`: o que falta, por quê e que tipo de fonte."""
    if result.request_id != request.request_id or result.route != "SOURCE_GAP":
        raise _err("NOT_A_SOURCE_GAP", "o resultado não é deste pedido ou não foi roteado a SOURCE_GAP")
    data = result.data
    gap = data.get("source_gap")
    if gap is not None:
        missing, because = list(gap["missing"]), gap["insufficient_because"]
    else:
        missing = ["resolução das afirmações: " + ", ".join(data["inconclusive"]["unresolved"])]
        because = data["rationale"]
    return {
        "kind": "source-gap-entry",
        "request_id": result.request_id,
        "result_id": result.result_id,
        "subject": _subject(request),
        "claims": sorted(item.claim_ref for item in request.claims),
        "missing": missing,
        "insufficient_because": because,
        "needed_source": result.routing.get("needed_source"),
        "evidence_consulted": list(data["evidence_used"]),
    }


def human_review_candidate(request: AIRecheckRequest, result: AIRecheckResult) -> dict:
    """Candidato a revisão humana. AI_RECHECK NÃO pergunta a ninguém: só entrega este registro à capacidade de revisão humana."""
    if result.request_id != request.request_id or result.route not in ("HUMAN_DECISION", "HUMAN_VALIDATION"):
        raise _err("NOT_A_HUMAN_CANDIDATE", "o resultado não é deste pedido ou não foi roteado a uma rota humana")
    data = result.data
    prior = request.prior_interpretation
    decision = data.get("decision")
    if decision is not None:
        question, options = decision["question"], list(decision["options"])
    elif result.route == "HUMAN_VALIDATION" and prior is not None:
        question, options = "Confirmar a interpretação: " + prior.summary, []
    else:
        question, options = request.ambiguity, []
    return {
        "kind": "human-review-candidate",
        "route": result.route,
        "state": "OPEN",
        "request_id": result.request_id,
        "result_id": result.result_id,
        "subject": _subject(request),
        "claims": sorted(item.claim_ref for item in request.claims),
        "question": question,
        "options": options,
        "hypothesis": prior.summary if prior is not None else None,
        "attempted_before": [{"step": "ai-recheck", "outcome": data["rationale"]}],
    }
