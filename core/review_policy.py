"""Política de roteamento de revisão: dado, versionado e validado. Nenhuma decisão mora aqui; só o que a política PODE dizer.

A política (`policies/review-routing.policy.json`) diz, para cada família de sinal (e opcionalmente cada qualificador), qual AÇÃO o
sinal pede em cada criticidade efetiva. Também guarda os pisos de criticidade por classe de impacto, o mapa de efeitos genéricos
para classe de impacto, o orçamento de releitura por IA, as taxas de amostragem e as suposições proibidas a quem decide. A ORDEM
entre as classes de revisão não está aqui: é fixa no roteador, para que nenhuma política consiga esconder uma decisão humana atrás
de uma amostra.

Regras que `ReviewPolicy` impõe ao carregar (qualquer violação recusa a política inteira; nada é aceito pela metade):

1. forma estrita: chaves desconhecidas são recusadas, e toda chave obrigatória precisa existir;
2. monotonia: criticidade maior nunca recebe uma ação MENOS severa (`OBSERVE < SAMPLE_QA < AI_RECHECK = SOURCE_GAP < HUMAN_DECISION`);
   o mesmo vale para os pisos de impacto (classe mais grave, piso não menor) e para a escalada de uma releitura inconclusiva;
3. uma ação `HUMAN_DECISION` exige `decision_kind`, que liga a regra às suposições proibidas;
4. a escalada de uma releitura inconclusiva nunca é `AI_RECHECK` (repetir a análise é laço, não contingência) nem `OBSERVE`;
5. `UNVERIFIED` nunca inclui `VALIDATED`: o que uma pessoa validou não é "não verificado".

Identidade: `policy.ref()` = `{policy_id, version, digest}`. O `digest` é o hash canônico de TODO o conteúdo, então qualquer
mudança (mesmo sem trocar a versão) muda a referência. Toda decisão de revisão grava essa referência; trocar a política invalida
as decisões calculadas com a anterior. `stage_parameters()` entrega o mesmo dado para a `RunConfig` de um estágio de orquestração,
de modo que o motor de estágios refaça o que dependia da política antiga (`CONFIG_CHANGED`).

Este módulo não decide nada sobre nenhum software e não lê relógio, rede ou ambiente. Só `load_policy` lê um arquivo, e só quando
chamada; o roteador recebe a política já carregada.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .analysis_signal import (
    ACTIONS,
    CRITICALITIES,
    DECISION_KINDS,
    EPISTEMIC_STATUSES,
    SOURCE_TYPES,
    ReviewContractError,
    check_effect,
    check_family,
    check_impact_class,
    check_qualifier,
    criticality_rank,
)
from .hashing import hash_canonical_json
from .model_task import BOUNDARIES, RISK_LEVELS, VERSION_PATTERN, ModelContractError, check_choice, check_name, check_text, is_int
from .serialization import CanonicalJsonError, canonical_bytes, loads_strict

POLICY_KIND = "review-routing-policy"
POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "review-routing.policy.json"
PARAMETER_KEY = "review_routing"
RULE_DEFAULT = "default"

# Severidade das ações, só para conferir monotonia. AI_RECHECK e SOURCE_GAP não se comparam entre si: são caminhos diferentes.
ACTION_SEVERITY = {"OBSERVE": 0, "SAMPLE_QA": 1, "AI_RECHECK": 2, "SOURCE_GAP": 2, "HUMAN_DECISION": 3}
# O que uma releitura inconclusiva pode virar. Nunca AI_RECHECK (laço) e nunca OBSERVE (esconderia a falta de conclusão).
ESCALATION_ACTIONS = ("SAMPLE_QA", "SOURCE_GAP", "HUMAN_DECISION")
SAMPLING_CLASSES = ("SAMPLE_QA", "AUTO_PASS")
MAX_ROUNDS_LIMIT = 5
_TOP_REQUIRED = {
    "kind", "policy_id", "version", "blocking_from", "rules", "default_rule", "impact", "recheck", "source_gap", "sampling",
    "prohibited_assumptions",
}
_TOP_OPTIONAL = {"principle", "precedence_note"}
_RULE_KEYS = {"description", "actions", "decision_kind"}
_IMPACT_KEYS = {"order", "floors", "high_impact", "unverified_statuses", "effects", "default_effect_impact"}
_RECHECK_KEYS = {"max_rounds", "default_max_boundary", "on_inconclusive", "risk_by_criticality"}
_VERSION = re.compile(VERSION_PATTERN)
_MAX_ISSUES = 8


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


@dataclass(frozen=True)
class Rule:
    """A regra escolhida para um sinal: de onde veio (`key`), o que pede por criticidade e, se houver, para que serve a decisão."""

    key: str
    actions: "tuple[tuple[str, str], ...]"
    decision_kind: "str | None" = None
    fallback: bool = False

    def action(self, criticality: str) -> str:
        return dict(self.actions)[check_choice(criticality, CRITICALITIES, "criticality")]


# ------------------------------------------------------------------------------------------------------------ validação
def _check_actions(actions: object, where: str, allowed: "tuple[str, ...]", problems: "list[str]") -> bool:
    if not isinstance(actions, dict) or set(actions) != set(CRITICALITIES):
        problems.append(f"{where}: deve ter exatamente as criticidades {list(CRITICALITIES)}")
        return False
    ok = True
    for criticality in CRITICALITIES:
        if actions[criticality] not in allowed:
            problems.append(f"{where}.{criticality}: ação deve ser uma de {list(allowed)}: {actions[criticality]!r}")
            ok = False
    if ok:
        ranks = [ACTION_SEVERITY[actions[criticality]] for criticality in CRITICALITIES]
        if ranks != sorted(ranks):
            problems.append(f"{where}: criticidade maior não pode receber ação menos severa ({[actions[c] for c in CRITICALITIES]})")
            ok = False
    return ok


def _check_rule(key: str, rule: object, problems: "list[str]") -> None:
    where = f"rules.{key}"
    if not isinstance(rule, dict) or set(rule) - _RULE_KEYS or "actions" not in rule:
        problems.append(f"{where}: regra deve ter actions e, no máximo, description e decision_kind")
        return
    if "description" in rule and not (isinstance(rule["description"], str) and rule["description"].strip()):
        problems.append(f"{where}.description: deve ser texto não vazio")
    ok = _check_actions(rule["actions"], f"{where}.actions", ACTIONS, problems)
    kind = rule.get("decision_kind")
    if kind is not None and kind not in DECISION_KINDS:
        problems.append(f"{where}.decision_kind: deve ser um de {list(DECISION_KINDS)}: {kind!r}")
    if ok and "HUMAN_DECISION" in rule["actions"].values() and kind is None:
        problems.append(f"{where}: uma regra que pode pedir HUMAN_DECISION precisa de decision_kind")


def _check_rule_key(key: object, problems: "list[str]") -> None:
    family, _, qualifier = str(key).partition(":")
    try:
        check_family(family)
        if ":" in str(key):
            check_qualifier(qualifier)
    except ModelContractError as exc:
        problems.append(f"rules.{key}: chave inválida ({exc})")


def _text_list(value: object, where: str, problems: "list[str]") -> None:
    if not isinstance(value, list) or not value:
        problems.append(f"{where}: deve ser lista não vazia")
        return
    for index, item in enumerate(value):
        try:
            check_text(item, f"{where}[{index}]")
        except ModelContractError as exc:
            problems.append(str(exc))


def _check_impact(impact: object, problems: "list[str]") -> None:
    if not isinstance(impact, dict) or set(impact) != _IMPACT_KEYS:
        problems.append(f"impact: deve ter exatamente as chaves {sorted(_IMPACT_KEYS)}")
        return
    order = impact["order"]
    if not isinstance(order, list) or not order or len(set(order)) != len(order):
        problems.append("impact.order: deve ser lista não vazia sem repetição")
        return
    try:
        for name in order:
            check_impact_class(name)
    except ModelContractError as exc:
        problems.append(f"impact.order: {exc}")
        return
    floors = impact["floors"]
    if not isinstance(floors, dict) or set(floors) != set(order) or any(value not in CRITICALITIES for value in floors.values()):
        problems.append("impact.floors: deve ter uma criticidade válida para cada classe de impacto")
    else:
        ranks = [criticality_rank(floors[name]) for name in order]
        if ranks != sorted(ranks):
            problems.append("impact.floors: classe mais grave não pode ter piso menor (impact.order é crescente em gravidade)")
    high = impact["high_impact"]
    if not isinstance(high, list) or len(set(high)) != len(high) or not set(high) <= set(order):
        problems.append("impact.high_impact: deve ser lista, sem repetição, de classes de impact.order")
    statuses = impact["unverified_statuses"]
    if not isinstance(statuses, list) or len(set(statuses)) != len(statuses) or not set(statuses) <= set(EPISTEMIC_STATUSES):
        problems.append("impact.unverified_statuses: deve ser lista, sem repetição, de status epistêmicos")
    elif "VALIDATED" in statuses:
        problems.append("impact.unverified_statuses: VALIDATED não é 'não verificado'")
    effects = impact["effects"]
    if not isinstance(effects, dict):
        problems.append("impact.effects: deve ser objeto efeito -> classe de impacto")
    else:
        for effect, target in effects.items():
            try:
                check_effect(effect)
            except ModelContractError as exc:
                problems.append(f"impact.effects: {exc}")
            if target not in order:
                problems.append(f"impact.effects.{effect}: classe de impacto desconhecida: {target!r}")
    if impact["default_effect_impact"] not in order:
        problems.append("impact.default_effect_impact: deve ser uma classe de impact.order")


def _check_recheck(recheck: object, problems: "list[str]") -> None:
    if not isinstance(recheck, dict) or set(recheck) != _RECHECK_KEYS:
        problems.append(f"recheck: deve ter exatamente as chaves {sorted(_RECHECK_KEYS)}")
        return
    rounds = recheck["max_rounds"]
    if not is_int(rounds) or not 1 <= rounds <= MAX_ROUNDS_LIMIT:
        problems.append(f"recheck.max_rounds: deve ser inteiro de 1 a {MAX_ROUNDS_LIMIT}")
    if recheck["default_max_boundary"] not in BOUNDARIES:
        problems.append(f"recheck.default_max_boundary: deve ser um de {list(BOUNDARIES)}")
    _check_actions(recheck["on_inconclusive"], "recheck.on_inconclusive", ESCALATION_ACTIONS, problems)
    risk = recheck["risk_by_criticality"]
    if not isinstance(risk, dict) or set(risk) != set(CRITICALITIES) or any(value not in RISK_LEVELS for value in risk.values()):
        problems.append(f"recheck.risk_by_criticality: deve mapear cada criticidade para um de {list(RISK_LEVELS)}")


def policy_problems(data: object) -> "list[str]":
    """Todas as violações de `data` como política. Lista vazia = válida. Determinística e sem efeito."""
    if not isinstance(data, dict):
        return ["a política deve ser objeto"]
    problems: list[str] = []
    missing, extra = _TOP_REQUIRED - set(data), set(data) - _TOP_REQUIRED - _TOP_OPTIONAL
    if missing or extra:
        return [f"chaves da política não conferem (faltam: {sorted(missing)}; sobram: {sorted(extra)})"]
    if data["kind"] != POLICY_KIND:
        problems.append(f"kind deve ser {POLICY_KIND!r}")
    try:
        check_name(data["policy_id"], "policy_id")
    except ModelContractError as exc:
        problems.append(str(exc))
    if not (isinstance(data["version"], str) and _VERSION.match(data["version"])):
        problems.append(f"version deve casar com {VERSION_PATTERN}")
    for key in _TOP_OPTIONAL & set(data):
        if not (isinstance(data[key], str) and data[key].strip()):
            problems.append(f"{key}: deve ser texto não vazio")
    if data["blocking_from"] not in CRITICALITIES:
        problems.append(f"blocking_from deve ser uma de {list(CRITICALITIES)}")
    rules = data["rules"]
    if not isinstance(rules, dict) or not rules:
        problems.append("rules: deve ser objeto não vazio")
    else:
        for key, rule in rules.items():
            _check_rule_key(key, problems)
            _check_rule(key, rule, problems)
    _check_rule(RULE_DEFAULT, data["default_rule"], problems)
    _check_impact(data["impact"], problems)
    _check_recheck(data["recheck"], problems)
    source_gap = data["source_gap"]
    if not isinstance(source_gap, dict) or set(source_gap) != {"default_source_types"}:
        problems.append("source_gap: deve ter exatamente default_source_types")
    else:
        types = source_gap["default_source_types"]
        if not isinstance(types, list) or not types or len(set(types)) != len(types) or not set(types) <= set(SOURCE_TYPES):
            problems.append(f"source_gap.default_source_types: lista não vazia, sem repetição, de {list(SOURCE_TYPES)}")
    sampling = data["sampling"]
    if not isinstance(sampling, dict) or set(sampling) != {"seed", "rates"}:
        problems.append("sampling: deve ter exatamente seed e rates")
    else:
        if not (isinstance(sampling["seed"], str) and sampling["seed"].strip()):
            problems.append("sampling.seed: deve ser texto não vazio")
        rates = sampling["rates"]
        if not isinstance(rates, dict) or set(rates) != set(SAMPLING_CLASSES):
            problems.append(f"sampling.rates: deve ter exatamente {list(SAMPLING_CLASSES)}")
        else:
            for name, rate in rates.items():
                if not _is_number(rate) or not 0 <= rate <= 1:
                    problems.append(f"sampling.rates.{name}: deve ser número de 0 a 1")
    prohibited = data["prohibited_assumptions"]
    if not isinstance(prohibited, dict) or set(prohibited) != set(DECISION_KINDS):
        problems.append(f"prohibited_assumptions: deve ter exatamente {list(DECISION_KINDS)}")
    else:
        for kind in DECISION_KINDS:
            _text_list(prohibited[kind], f"prohibited_assumptions.{kind}", problems)
    return problems


# ----------------------------------------------------------------------------------------------------------- a política
@dataclass(frozen=True)
class ReviewPolicy:
    """Política validada e imutável (por convenção: os acessadores devolvem cópias). Construir confere tudo."""

    data: dict
    digest: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if not isinstance(self.data, dict):
            raise ReviewContractError("INVALID_POLICY", "a política deve ser objeto")
        try:
            data = loads_strict(canonical_bytes(self.data))
        except CanonicalJsonError as exc:
            raise ReviewContractError("INVALID_POLICY", f"a política não cabe no JSON canônico: {exc}") from exc
        problems = policy_problems(data)
        if problems:
            raise ReviewContractError("INVALID_POLICY", "política inválida: " + "; ".join(problems[:_MAX_ISSUES]))
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "digest", str(hash_canonical_json(data)))

    # -- identidade
    @property
    def policy_id(self) -> str:
        return self.data["policy_id"]

    @property
    def version(self) -> str:
        return self.data["version"]

    def ref(self) -> dict:
        """Referência estável da política: quem a usou grava isto. Muda com QUALQUER mudança de conteúdo."""
        return {"policy_id": self.policy_id, "version": self.version, "digest": self.digest}

    def stage_parameters(self) -> dict:
        """Parâmetros que a `RunConfig` deve declarar para um estágio que roteia por esta política."""
        return {PARAMETER_KEY: {"policy": self.ref()}}

    def to_dict(self) -> dict:
        return loads_strict(canonical_bytes(self.data))  # type: ignore[return-value]

    # -- regras
    def rule_for(self, family: str, qualifier: "str | None" = None) -> Rule:
        """Regra mais específica: `FAMILIA:qualificador`, depois `FAMILIA`, depois a regra padrão (`fallback=True`)."""
        rules = self.data["rules"]
        for key in ((f"{family}:{qualifier}",) if qualifier is not None else ()) + (family,):
            if key in rules:
                return self._rule(key, rules[key], False)
        return self._rule(RULE_DEFAULT, self.data["default_rule"], True)

    @staticmethod
    def _rule(key: str, rule: dict, fallback: bool) -> Rule:
        actions = tuple((criticality, rule["actions"][criticality]) for criticality in CRITICALITIES)
        return Rule(key, actions, rule.get("decision_kind"), fallback)

    def is_known_family(self, family: str) -> bool:
        return any(key == family or key.startswith(f"{family}:") for key in self.data["rules"])

    @property
    def blocking_from(self) -> str:
        return self.data["blocking_from"]

    def is_blocking(self, criticality: str) -> bool:
        """A partir daqui, uma lacuna de fonte bloqueia (passa à frente de AI_RECHECK)."""
        return criticality_rank(criticality) >= criticality_rank(self.blocking_from)

    # -- impacto
    @property
    def impact_order(self) -> "tuple[str, ...]":
        return tuple(self.data["impact"]["order"])

    def knows_impact(self, impact_class: str) -> bool:
        return impact_class in self.data["impact"]["order"]

    def impact_rank(self, impact_class: str) -> int:
        order = self.data["impact"]["order"]
        if impact_class not in order:
            raise ReviewContractError("UNKNOWN_IMPACT", f"classe de impacto desconhecida da política: {impact_class!r}")
        return order.index(impact_class)

    def impact_floor(self, impact_class: str) -> str:
        """Criticidade mínima que a classe de impacto impõe a qualquer sinal sobre a afirmação."""
        self.impact_rank(impact_class)
        return self.data["impact"]["floors"][impact_class]

    def is_high_impact(self, impact_class: str) -> bool:
        return impact_class in self.data["impact"]["high_impact"]

    def derive_impact(self, impact_class: "str | None", effects: Iterable[str] = ()) -> str:
        """Classe de impacto: a MAIOR entre a declarada e as derivadas dos efeitos. Efeito desconhecido NÃO é inofensivo:
        cai em `default_effect_impact`. Sem nada declarado, a classe mais baixa. Classe declarada desconhecida é recusada."""
        impact = self.data["impact"]
        found: list[str] = []
        if impact_class is not None:
            self.impact_rank(impact_class)
            found.append(impact_class)
        for effect in effects:
            found.append(impact["effects"].get(effect, impact["default_effect_impact"]))
        if not found:
            return impact["order"][0]
        return max(found, key=impact["order"].index)

    def is_unverified(self, status: str) -> bool:
        return status in self.data["impact"]["unverified_statuses"]

    # -- releitura por IA
    @property
    def max_rounds(self) -> int:
        return self.data["recheck"]["max_rounds"]

    @property
    def default_max_boundary(self) -> str:
        return self.data["recheck"]["default_max_boundary"]

    def on_inconclusive(self, criticality: str) -> str:
        """Para onde uma releitura inconclusiva (sem rodadas sobrando) escala, pela criticidade efetiva. Nunca AI_RECHECK."""
        return self.data["recheck"]["on_inconclusive"][check_choice(criticality, CRITICALITIES, "criticality")]

    def risk_level(self, criticality: str) -> str:
        return self.data["recheck"]["risk_by_criticality"][check_choice(criticality, CRITICALITIES, "criticality")]

    # -- lacuna de fonte, amostragem, suposições proibidas
    @property
    def default_source_types(self) -> "tuple[str, ...]":
        return tuple(sorted(self.data["source_gap"]["default_source_types"]))

    @property
    def sampling_seed(self) -> str:
        return self.data["sampling"]["seed"]

    def sampling_rate(self, review_class: str) -> "float | None":
        """Taxa de amostragem da classe, ou `None` se a classe não é amostrada (as demais têm cobertura total ou nenhuma)."""
        return self.data["sampling"]["rates"].get(review_class)

    def prohibited_assumptions(self, decision_kind: str) -> "tuple[str, ...]":
        return tuple(self.data["prohibited_assumptions"][check_choice(decision_kind, DECISION_KINDS, "decision_kind")])


# --------------------------------------------------------------------------------------------------------------- carga
def load_policy(path: "Path | str | None" = None) -> ReviewPolicy:
    """Lê e valida uma política. Único ponto de leitura de arquivo deste módulo; o roteador nunca chama isto."""
    target = Path(path) if path is not None else POLICY_PATH
    try:
        data = loads_strict(target.read_bytes())
    except (OSError, CanonicalJsonError) as exc:
        raise ReviewContractError("UNREADABLE_POLICY", f"não foi possível ler a política ({type(exc).__name__})") from exc
    return ReviewPolicy(data)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def default_policy() -> ReviewPolicy:
    """A política padrão do repositório, carregada uma vez."""
    return load_policy()
