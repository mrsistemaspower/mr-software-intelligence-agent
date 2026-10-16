"""Ciclo de vida de um estágio: estados, transições permitidas e registro de estado.

Seis estados, o mínimo que distingue o que a retomada precisa distinguir:

    PENDING      ainda não executou (ou foi reposto para tentar de novo)
    RUNNING      uma tentativa está em curso; se o processo some, o registro fica assim no disco
    COMPLETED    concluiu e tem checkpoint; só é pulado se o checkpoint ainda for válido
    FAILED       a última tentativa falhou; carrega a falha registrada
    BLOCKED      não pode rodar porque uma dependência falhou
    INVALIDATED  estava concluído, mas o resultado deixou de valer; precisa rodar de novo

`READY` não é estado persistido: é derivado. Um estágio está pronto quando está em PENDING ou INVALIDATED e todas as
suas dependências estão em COMPLETED (ver `is_runnable`). Persistir o derivado só criaria uma segunda verdade para manter
em sincronia. `INVALIDATED` existe separado de `PENDING` porque guarda a causa e distingue "nunca rodou" de "rodou e deixou
de valer", que é a pergunta central da retomada.

Transições permitidas (qualquer outra levanta `TransitionError`):

    PENDING     -> RUNNING | BLOCKED
    RUNNING     -> COMPLETED | FAILED | PENDING (só com recovery=True: recuperação de interrupção)
    COMPLETED   -> INVALIDATED
    FAILED      -> PENDING (nova tentativa ou entradas mudaram) | BLOCKED
    BLOCKED     -> PENDING
    INVALIDATED -> RUNNING | BLOCKED

Um estágio COMPLETED nunca volta direto a PENDING nem a RUNNING: passa por INVALIDATED, com causa registrada.
A falha é dado portátil (texto e números): nenhuma exceção do Python entra no contrato.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from .paths import find_local_paths
from .stage_graph import is_valid_name


class TransitionError(ValueError):
    """Transição de estado inválida, ou registro que viola o invariante do seu estado."""


class StageState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INVALIDATED = "INVALIDATED"


_ALLOWED: dict[StageState, frozenset[StageState]] = {
    StageState.PENDING: frozenset({StageState.RUNNING, StageState.BLOCKED}),
    StageState.RUNNING: frozenset({StageState.COMPLETED, StageState.FAILED, StageState.PENDING}),
    StageState.COMPLETED: frozenset({StageState.INVALIDATED}),
    StageState.FAILED: frozenset({StageState.PENDING, StageState.BLOCKED}),
    StageState.BLOCKED: frozenset({StageState.PENDING}),
    StageState.INVALIDATED: frozenset({StageState.RUNNING, StageState.BLOCKED}),
}
_RECOVERY_ONLY = frozenset({(StageState.RUNNING, StageState.PENDING)})

TRANSITIONS: dict[str, tuple[str, ...]] = {
    state.value: tuple(sorted(target.value for target in targets)) for state, targets in _ALLOWED.items()
}

# Categorias de falha (fechadas, para que a política decida sem interpretar texto).
HANDLER_ERROR = "HANDLER_ERROR"
INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
INPUT_MUTATED = "INPUT_MUTATED"
OUTPUT_CONTRACT = "OUTPUT_CONTRACT"
INTERRUPTED = "INTERRUPTED"
SOURCE_MUTATION = "SOURCE_MUTATION"
FAILURE_CATEGORIES = (HANDLER_ERROR, INPUT_UNAVAILABLE, INPUT_MUTATED, OUTPUT_CONTRACT, INTERRUPTED, SOURCE_MUTATION)

CHECKPOINT_ABSENT = "ABSENT"
CHECKPOINT_VALID = "VALID"
CHECKPOINT_INVALID = "INVALID"
CHECKPOINT_STATES = (CHECKPOINT_ABSENT, CHECKPOINT_VALID, CHECKPOINT_INVALID)

# Causas de invalidação e de bloqueio (fechadas).
UPSTREAM_INVALIDATED = "UPSTREAM_INVALIDATED"
UPSTREAM_FAILED = "UPSTREAM_FAILED"
CHECKPOINT_MISSING = "CHECKPOINT_MISSING"
CHECKPOINT_CORRUPT = "CHECKPOINT_CORRUPT"
INVALIDATION_CODES = (
    UPSTREAM_INVALIDATED, CHECKPOINT_MISSING, CHECKPOINT_CORRUPT,
    "WORKFLOW_CHANGED", "DEFINITION_CHANGED", "SNAPSHOT_CHANGED", "CONFIG_CHANGED", "INPUT_CHANGED",
    "UPSTREAM_CHANGED", "OUTPUT_SET_CHANGED", "OUTPUT_MISSING", "OUTPUT_MUTATED", "OUTPUT_UNREADABLE",
)
CAUSE_CODES = INVALIDATION_CODES + (UPSTREAM_FAILED,)

_MESSAGE_LIMIT = 400
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_REDACTED = "<detalhe omitido: a mensagem continha um caminho local>"
_IDENTITY = re.compile(r"^art:[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*:sha256:[0-9a-f]{64}$")
_CHECKPOINT_ID = re.compile(r"^ckpt:sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class FailureInfo:
    """O que se registra de uma falha: dados simples, sem exceção do Python e sem caminho local.

    `retryable`: uma nova tentativa com as mesmas entradas poderia dar certo? O padrão é `True` (o motor sempre repetiu até o
    limite da `RetryPolicy`). `False` marca uma falha determinística (por exemplo, adaptador inelegível ou configuração estática
    malformada): repetir só gasta tentativa, então a retomada não a repete. Só o valor `False` é gravado; o padrão não aparece no
    dado, para que o formato anterior continue idêntico.
    """

    stage_id: str
    category: str
    message: str
    attempt: int
    checkpoint_state: str = CHECKPOINT_ABSENT
    retryable: bool = True

    def __post_init__(self) -> None:
        if not is_valid_name(self.stage_id):
            raise TransitionError("falha com stage_id inválido")
        if self.category not in FAILURE_CATEGORIES:
            raise TransitionError(f"categoria de falha desconhecida: {self.category!r}")
        if not (isinstance(self.message, str) and 0 < len(self.message) <= _MESSAGE_LIMIT) or _CONTROL.search(self.message):
            raise TransitionError(f"a mensagem de falha deve ter de 1 a {_MESSAGE_LIMIT} caracteres, sem controles")
        if find_local_paths(self.message):
            raise TransitionError("a mensagem de falha não pode carregar caminho local")
        if type(self.attempt) is not int or self.attempt < 0:
            raise TransitionError("attempt deve ser inteiro não negativo")
        if self.checkpoint_state not in CHECKPOINT_STATES:
            raise TransitionError(f"checkpoint_state desconhecido: {self.checkpoint_state!r}")
        if type(self.retryable) is not bool:
            raise TransitionError("retryable deve ser booleano")

    def to_dict(self) -> dict:
        data: dict = {
            "stage_id": self.stage_id,
            "category": self.category,
            "message": self.message,
            "attempt": self.attempt,
            "checkpoint_state": self.checkpoint_state,
        }
        if not self.retryable:
            data["retryable"] = False
        return data

    @classmethod
    def from_dict(cls, data: object) -> "FailureInfo":
        keys = {"stage_id", "category", "message", "attempt", "checkpoint_state"}
        if not isinstance(data, dict) or not keys <= set(data) or set(data) - keys - {"retryable"}:
            raise TransitionError("falha malformada")
        if "retryable" in data and data["retryable"] is not False:
            # Uma só representação: o padrão (repetível) não é gravado, então `true` explícito é dado fora do formato.
            raise TransitionError("retryable só é gravado quando é false")
        return cls(
            data["stage_id"], data["category"], data["message"], data["attempt"], data["checkpoint_state"], data.get("retryable", True)
        )


def sanitize_message(text: str) -> str:
    """Mensagem segura para um contrato portátil: sem controles, limitada, e sem caminho local (omite o detalhe todo)."""
    cleaned = _CONTROL.sub(" ", str(text)).strip() or "falha sem mensagem"
    if find_local_paths(cleaned):
        return _REDACTED
    return cleaned[:_MESSAGE_LIMIT]


def failure_from_exception(
    stage_id: str, error: BaseException, attempt: int, *, category: str = HANDLER_ERROR, checkpoint_state: str = CHECKPOINT_ABSENT
) -> FailureInfo:
    """Resume uma exceção em dados portáteis (nome do tipo e mensagem limpa). A exceção em si nunca é gravada.

    Uma exceção qualquer é repetível: o motor não sabe se uma nova tentativa daria certo, então mantém o comportamento de sempre.
    Só um tratador que SABE que a falha é determinística diz o contrário, levantando `StageFailure(..., retryable=False)`.
    """
    detail = str(error).strip()
    text = type(error).__name__ + (f": {detail}" if detail else "")
    return FailureInfo(stage_id, category, sanitize_message(text), attempt, checkpoint_state)


@dataclass(frozen=True)
class Cause:
    """Por que um estágio foi invalidado ou bloqueado. `stage_id` é o estágio de origem; `detail`, a entrada ou saída."""

    code: str
    stage_id: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.code not in CAUSE_CODES:
            raise TransitionError(f"causa desconhecida: {self.code!r}")
        if self.stage_id is not None and not is_valid_name(self.stage_id):
            raise TransitionError("causa com stage_id inválido")
        if self.detail is not None and not (isinstance(self.detail, str) and 0 < len(self.detail) <= 200):
            raise TransitionError("detail da causa deve ter de 1 a 200 caracteres")
        if self.detail is not None and (_CONTROL.search(self.detail) or find_local_paths(self.detail)):
            raise TransitionError("detail da causa não pode ter controle nem caminho local")

    def to_dict(self) -> dict:
        data: dict = {"code": self.code}
        if self.stage_id is not None:
            data["stage_id"] = self.stage_id
        if self.detail is not None:
            data["detail"] = self.detail
        return data

    @classmethod
    def from_dict(cls, data: object) -> "Cause":
        if not isinstance(data, dict) or "code" not in data or set(data) - {"code", "stage_id", "detail"}:
            raise TransitionError("causa malformada")
        return cls(data["code"], data.get("stage_id"), data.get("detail"))


def check_transition(current: StageState, target: StageState, *, recovery: bool = False) -> None:
    """Levanta `TransitionError` se `current -> target` não for permitida."""
    if not isinstance(current, StageState) or not isinstance(target, StageState):
        raise TransitionError("os estados devem ser StageState")
    if target not in _ALLOWED[current]:
        raise TransitionError(f"transição inválida: {current.value} -> {target.value}")
    if (current, target) in _RECOVERY_ONLY and not recovery:
        raise TransitionError(f"{current.value} -> {target.value} só vale como recuperação de interrupção (recovery=True)")


def is_runnable(state: StageState, dependency_states: "list[StageState] | tuple[StageState, ...]") -> bool:
    """O estágio está pronto (READY derivado)? Está em PENDING ou INVALIDATED e todas as dependências estão COMPLETED."""
    return state in (StageState.PENDING, StageState.INVALIDATED) and all(
        item is StageState.COMPLETED for item in dependency_states
    )


@dataclass(frozen=True)
class StageRecord:
    """Estado de um estágio em uma execução. Imutável: `transition` devolve um novo registro."""

    stage_id: str
    state: StageState = StageState.PENDING
    attempts: int = 0
    failure: FailureInfo | None = None
    cause: Cause | None = None
    checkpoint_id: str | None = None
    last_identity: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_name(self.stage_id):
            raise TransitionError("stage_id inválido")
        if not isinstance(self.state, StageState):
            raise TransitionError("state deve ser StageState")
        if type(self.attempts) is not int or self.attempts < 0:
            raise TransitionError("attempts deve ser inteiro não negativo")
        if self.checkpoint_id is not None and not (isinstance(self.checkpoint_id, str) and _CHECKPOINT_ID.match(self.checkpoint_id)):
            raise TransitionError("checkpoint_id inválido")
        if self.last_identity is not None and not (isinstance(self.last_identity, str) and _IDENTITY.match(self.last_identity)):
            raise TransitionError("last_identity inválida")
        if self.failure is not None and self.failure.stage_id != self.stage_id:
            raise TransitionError("a falha pertence a outro estágio")
        state = self.state
        if state is StageState.COMPLETED:
            ok = self.checkpoint_id is not None and self.last_identity is not None and self.failure is None and self.cause is None
        elif state is StageState.FAILED:
            ok = self.failure is not None and self.checkpoint_id is None and self.cause is None
        elif state in (StageState.BLOCKED, StageState.INVALIDATED):
            ok = self.cause is not None and self.failure is None and self.checkpoint_id is None
            if ok and state is StageState.BLOCKED:
                ok = self.cause.code == UPSTREAM_FAILED
            if ok and state is StageState.INVALIDATED:
                ok = self.cause.code in INVALIDATION_CODES
        elif state is StageState.RUNNING:
            ok = self.attempts >= 1 and self.failure is None and self.cause is None and self.checkpoint_id is None
        else:  # PENDING
            ok = self.failure is None and self.cause is None and self.checkpoint_id is None and self.last_identity is None
        if not ok:
            raise TransitionError(f"registro viola o invariante do estado {state.value} (estágio {self.stage_id})")

    def transition(
        self,
        target: StageState,
        *,
        failure: FailureInfo | None = None,
        cause: Cause | None = None,
        checkpoint_id: str | None = None,
        last_identity: str | None = None,
        reset_attempts: bool = False,
        refund_attempt: bool = False,
        recovery: bool = False,
    ) -> "StageRecord":
        """Novo registro no estado `target`. Verifica a transição e o invariante do estado de chegada.

        Contagem de tentativas: entrar em RUNNING conta uma tentativa; `refund_attempt` (só em recuperação de
        interrupção) devolve a tentativa interrompida; `reset_attempts` zera (entradas novas, vida nova);
        COMPLETED -> INVALIDATED zera sempre, porque o resultado antigo não pertence mais às entradas atuais.
        """
        check_transition(self.state, target, recovery=recovery)
        attempts = self.attempts
        if target is StageState.RUNNING:
            attempts += 1
        if refund_attempt:
            if not recovery:
                raise TransitionError("refund_attempt só vale em recuperação de interrupção")
            attempts = max(0, attempts - 1)
        if reset_attempts or (self.state is StageState.COMPLETED and target is StageState.INVALIDATED):
            attempts = 0
        keep_identity = last_identity if target in (StageState.RUNNING, StageState.COMPLETED) else None
        if target is StageState.FAILED:
            keep_identity = self.last_identity
        if target is StageState.COMPLETED and keep_identity is None:
            keep_identity = self.last_identity
        return StageRecord(
            stage_id=self.stage_id,
            state=target,
            attempts=attempts,
            failure=failure if target is StageState.FAILED else None,
            cause=cause if target in (StageState.BLOCKED, StageState.INVALIDATED) else None,
            checkpoint_id=checkpoint_id if target is StageState.COMPLETED else None,
            last_identity=keep_identity,
        )

    def with_(self, **changes: object) -> "StageRecord":
        return replace(self, **changes)

    def to_dict(self) -> dict:
        data: dict = {"stage_id": self.stage_id, "state": self.state.value, "attempts": self.attempts}
        if self.failure is not None:
            data["failure"] = self.failure.to_dict()
        if self.cause is not None:
            data["cause"] = self.cause.to_dict()
        if self.checkpoint_id is not None:
            data["checkpoint_id"] = self.checkpoint_id
        if self.last_identity is not None:
            data["last_identity"] = self.last_identity
        return data

    @classmethod
    def from_dict(cls, data: object) -> "StageRecord":
        allowed = {"stage_id", "state", "attempts", "failure", "cause", "checkpoint_id", "last_identity"}
        if not isinstance(data, dict) or not {"stage_id", "state", "attempts"} <= set(data) or set(data) - allowed:
            raise TransitionError("registro de estágio malformado")
        try:
            state = StageState(data["state"])
        except ValueError:
            raise TransitionError(f"estado desconhecido: {data['state']!r}") from None
        return cls(
            stage_id=data["stage_id"],
            state=state,
            attempts=data["attempts"],
            failure=FailureInfo.from_dict(data["failure"]) if "failure" in data else None,
            cause=Cause.from_dict(data["cause"]) if "cause" in data else None,
            checkpoint_id=data.get("checkpoint_id"),
            last_identity=data.get("last_identity"),
        )
