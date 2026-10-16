"""AI_RECHECK como tratador de estágio do orquestrador da Extração 2. Não é outro framework de orquestração.

`AIRecheckStage` é um tratador comum (`handler(ctx) -> None`). Ele:

1. confere que os PARÂMETROS do estágio declaram este adaptador e esta política. Assim a identidade do checkpoint amarra o
   modelo: trocar de adaptador, de versão, de modelo ou de política invalida o recheck já concluído (`CONFIG_CHANGED`), em vez
   de pular em silêncio um resultado de outro modelo. Use `stage_parameters()` ao montar a `RunConfig`;
2. carrega o pedido (de uma ENTRADA declarada do estágio, ou da saída de uma dependência) e o confere (`request_id`);
3. lê a evidência pelo `ctx` (só o que o estágio declarou: `input:<nome>` ou `upstream:<estágio>/<saída>`), de modo que a
   evidência autorizada também é, por construção, entrada do checkpoint, e a mudança dela invalida o estágio;
4. chama `run_recheck` (elegibilidade, integridade da evidência, tarefa, adaptador, validação, rota);
5. falha técnica -> `StageFailure` com categoria do motor de estágios (`INPUT_MUTATED` se a evidência mudou, `INPUT_UNAVAILABLE`
   se não pôde ser lida, `HANDLER_ERROR` no resto) e com o `retryable` que o executor decidiu. Repetível: a repetição segue a
   `RetryPolicy` do estágio, em uma nova retomada, nunca em laço aqui; atingido o limite, o estágio fica `FAILED`. NÃO repetível
   (adaptador inelegível, violação da política de provedor, capacidade ausente, configuração estática malformada): o estágio fica
   `FAILED` já na primeira tentativa e a retomada não gasta outra;
6. resultado válido (inclusive `SOURCE_GAP`, `HUMAN_DECISION` e `INCONCLUSIVE`) -> grava o resultado como saída declarada. O estágio
   fica `COMPLETED`: um veredito de recheck não é falha do estágio. A ROTA vai dentro do resultado (`routing`), e quem consome
   decide o que fazer; o checkpoint é gravado pelo orquestrador.

Não há tentativa de repetir o recheck por conta própria: falha técnica e `INCONCLUSIVE` válido são coisas diferentes e só a
primeira é repetida.

Limites: o estágio lê a evidência pelo contrato do `ctx`, então ela precisa ser entrada declarada ou saída de dependência
direta. O resultado gravado tem proveniência do adaptador, não conteúdo bruto de evidência.
"""
from __future__ import annotations

from .ai_recheck import (
    EVIDENCE_CHANGED,
    EVIDENCE_UNAVAILABLE,
    EVIDENCE_UNDECODABLE,
    FAILED,
    AIRecheckRequest,
    RecheckPolicy,
)
from .model_adapter import ModelAdapter
from .model_task import EvidenceRef, ModelContractError
from .orchestrator import InputMutatedError, StageContext, StageFailure
from .recheck_engine import EvidenceChanged, EvidenceUnavailable, run_recheck
from .serialization import CanonicalJsonError, loads_strict
from .stage_graph import is_valid_name
from .stage_state import HANDLER_ERROR, INPUT_MUTATED, INPUT_UNAVAILABLE

PARAMETER_KEY = "ai_recheck"
INPUT_PREFIX = "input:"
UPSTREAM_PREFIX = "upstream:"
DEFAULT_OUTPUT = "result"

_CATEGORY = {
    EVIDENCE_CHANGED: INPUT_MUTATED,
    EVIDENCE_UNAVAILABLE: INPUT_UNAVAILABLE,
    EVIDENCE_UNDECODABLE: INPUT_UNAVAILABLE,
}


def input_locator(name: str) -> str:
    """Locator de evidência que é uma ENTRADA declarada do estágio de recheck."""
    return f"{INPUT_PREFIX}{name}"


def upstream_locator(stage_id: str, name: str) -> str:
    """Locator de evidência que é uma saída de uma dependência direta do estágio de recheck."""
    return f"{UPSTREAM_PREFIX}{stage_id}/{name}"


class _StageEvidenceReader:
    """Lê evidência pelo `ctx`: só o que o estágio declarou. Mudança durante o estágio é `EvidenceChanged`."""

    def __init__(self, ctx: StageContext) -> None:
        self._ctx = ctx

    def read(self, evidence: EvidenceRef) -> bytes:
        locator = evidence.locator
        try:
            if locator is not None and locator.startswith(INPUT_PREFIX):
                return self._ctx.read_input(locator[len(INPUT_PREFIX):])
            if locator is not None and locator.startswith(UPSTREAM_PREFIX):
                stage_id, _, name = locator[len(UPSTREAM_PREFIX):].partition("/")
                return self._ctx.read_upstream(stage_id, name)
        except InputMutatedError as exc:
            raise EvidenceChanged(str(exc)) from exc
        except StageFailure as exc:
            raise EvidenceUnavailable(str(exc)) from exc
        raise EvidenceUnavailable("locator ausente ou não suportado pelo estágio")


class AIRecheckStage:
    """Tratador do estágio AI_RECHECK. Chamável: passe-o ao `Orchestrator` como tratador do estágio."""

    def __init__(
        self,
        adapter: ModelAdapter,
        *,
        request_input: "str | None" = None,
        request_upstream: "tuple[str, str] | None" = None,
        output: str = DEFAULT_OUTPUT,
        policy: "RecheckPolicy | None" = None,
    ) -> None:
        if (request_input is None) == (request_upstream is None):
            raise ValueError("informe exatamente uma origem do pedido: request_input ou request_upstream")
        if request_input is not None and not is_valid_name(request_input):
            raise ValueError("request_input não é um nome lógico válido")
        if request_upstream is not None and not (
            isinstance(request_upstream, tuple) and len(request_upstream) == 2 and all(is_valid_name(part) for part in request_upstream)
        ):
            raise ValueError("request_upstream deve ser (estágio, saída) com nomes lógicos válidos")
        if not is_valid_name(output):
            raise ValueError("output não é um nome lógico válido")
        self.adapter = adapter
        self.descriptor = adapter.descriptor
        self.request_input = request_input
        self.request_upstream = request_upstream
        self.output = output
        self.policy = policy if policy is not None else RecheckPolicy()

    def stage_parameters(self) -> dict:
        """Parâmetros que a `RunConfig` deve declarar para este estágio. Entram na identidade do checkpoint."""
        request = (
            {"via": "input", "name": self.request_input}
            if self.request_input is not None
            else {"via": "upstream", "stage": self.request_upstream[0], "name": self.request_upstream[1]}  # type: ignore[index]
        )
        return {
            PARAMETER_KEY: {
                "adapter": self.descriptor.identity_dict(),
                "policy": self.policy.to_dict(),
                "request": request,
                "output": self.output,
            }
        }

    def _load_request(self, ctx: StageContext) -> AIRecheckRequest:
        if self.request_input is not None:
            raw = ctx.read_input(self.request_input)
        else:
            assert self.request_upstream is not None
            raw = ctx.read_upstream(*self.request_upstream)
        try:
            return AIRecheckRequest.from_dict(loads_strict(raw))
        except (CanonicalJsonError, ModelContractError) as exc:
            # O mesmo pedido, lido de novo, seria recusado do mesmo jeito: repetir só gastaria tentativa.
            raise StageFailure(f"pedido de AI_RECHECK inválido: {exc}", HANDLER_ERROR, retryable=False) from exc

    def __call__(self, ctx: StageContext) -> None:
        if ctx.parameters.get(PARAMETER_KEY) != self.stage_parameters()[PARAMETER_KEY]:
            # Configuração estática malformada: determinística, não melhora com nova tentativa.
            raise StageFailure(
                "os parâmetros do estágio não declaram este adaptador e esta política; use AIRecheckStage.stage_parameters()",
                HANDLER_ERROR,
                retryable=False,
            )
        request = self._load_request(ctx)
        result = run_recheck(request, self.adapter, _StageEvidenceReader(ctx), policy=self.policy, attempt=ctx.attempt)
        if result.status == FAILED:
            failure = result.failure
            assert failure is not None
            # O executor já decidiu se uma nova tentativa poderia dar certo (`failure.retryable`); o motor de estágios respeita.
            raise StageFailure(
                f"{failure['kind']}: {failure['message']}", _CATEGORY.get(failure["kind"], HANDLER_ERROR), retryable=bool(failure["retryable"])
            )
        ctx.write_output_json(self.output, result.to_dict())
