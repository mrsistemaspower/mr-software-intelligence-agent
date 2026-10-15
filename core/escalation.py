"""Escalonamento: decide a rota de um item que o agente não consegue fechar sozinho.

Princípio: humanos respondem somente o que a IA não consegue resolver de maneira segura.
A função `decide` é pura e determinística. Ela só roteia; quem registra o que foi tentado é o agente,
em `human-review.attempted_before`.

Ordem de decisão:
  1. conhecimento já validado que se aplica -> sem escalonamento
  2. já VALIDATED -> sem escalonamento
  3. sem evidência no workspace -> SOURCE_GAP
  4. escolha entre alternativas que a evidência não decide -> HUMAN_DECISION
  5. leitura ambígua (confiança baixa) e ainda sem nova leitura -> AI_RECHECK
  6. leitura continua ambígua depois da nova leitura -> HUMAN_VALIDATION (há inferência a confirmar)
     ou SOURCE_GAP (não há o que confirmar; falta uma fonte, possivelmente um especialista)
  7. alto impacto, inferido e não validado -> HUMAN_VALIDATION
  8. caso contrário -> sem escalonamento
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .epistemics import DEFAULT_HIGH_FROM, DEFAULT_LOW_BELOW, band
from .schema_lite import Registry, Validator

POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "escalation.policy.json"
NO_ESCALATION = "NO_ESCALATION"

DEFAULT_SIGNALS = {
    "prior_validated_applies": False,
    "status": "INFERRED",
    "evidence_present": True,
    "source_obtainable": None,
    "confidence": 1.0,
    "recheck_done": False,
    "recheck_resolved": False,
    "choice_options": 0,
    "inference_to_confirm": False,
    "impact": "normal",
}


@dataclass(frozen=True)
class Decision:
    route: str
    reason: str
    needed_source: str | None = None

    @property
    def is_human(self) -> bool:
        return self.route in ("HUMAN_DECISION", "HUMAN_VALIDATION")


def load_policy(path: Path | str | None = None, registry: Registry | None = None) -> dict:
    policy = json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))
    errors = Validator(registry or Registry()).validate(policy, "escalation-policy")
    if errors:
        raise ValueError("política inválida: " + "; ".join(errors))
    return policy


def decide(signals: dict, policy: dict | None = None) -> Decision:
    unknown = set(signals) - set(DEFAULT_SIGNALS)
    if unknown:
        raise ValueError(f"sinais desconhecidos: {sorted(unknown)}")
    s = {**DEFAULT_SIGNALS, **signals}
    thresholds = (policy or {}).get("thresholds", {})
    low_below = thresholds.get("low_below", DEFAULT_LOW_BELOW)
    high_from = thresholds.get("high_from", DEFAULT_HIGH_FROM)
    high_requires_validation = (policy or {}).get("impact_rules", {}).get("high_requires_validation", True)

    if s["prior_validated_applies"]:
        return Decision(NO_ESCALATION, "reuse")
    if s["status"] == "VALIDATED":
        return Decision(NO_ESCALATION, "already-validated")
    if not s["evidence_present"]:
        return Decision("SOURCE_GAP", "no-evidence", needed_source=s["source_obtainable"] or "human-expert")
    if s["choice_options"] >= 2:
        return Decision("HUMAN_DECISION", "needs-choice")

    ambiguous = band(s["confidence"], low_below, high_from) == "low" and not (s["recheck_done"] and s["recheck_resolved"])
    if ambiguous and not s["recheck_done"]:
        return Decision("AI_RECHECK", "ambiguous-reading")
    if ambiguous:
        if s["inference_to_confirm"]:
            return Decision("HUMAN_VALIDATION", "recheck-unresolved")
        return Decision("SOURCE_GAP", "recheck-unresolved", needed_source=s["source_obtainable"] or "human-expert")
    if high_requires_validation and s["impact"] == "high" and s["inference_to_confirm"]:
        return Decision("HUMAN_VALIDATION", "high-impact-unvalidated")
    return Decision(NO_ESCALATION, "sufficient")


def _normalize(text: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", folded.casefold())).strip()


def dedupe_questions(reviews: list[dict]) -> list[list[dict]]:
    """Agrupa revisões que fazem a mesma pergunta. A chave é `family_key`, ou a pergunta normalizada.

    Cada grupo deve virar uma única pergunta ao humano; a resposta vale para todos os membros.
    """
    groups: dict[str, list[dict]] = {}
    for review in reviews:
        key = review.get("family_key") or _normalize(review["question"])
        groups.setdefault(key, []).append(review)
    return list(groups.values())
