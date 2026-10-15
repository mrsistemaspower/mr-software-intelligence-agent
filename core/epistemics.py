"""Regras epistêmicas que os schemas sozinhos não expressam.

Status (o que sustenta a afirmação) e confiança (quão certa ela é) são eixos independentes.
Este módulo não decide nada sobre nenhum software: só aplica as regras de disciplina de evidência.
"""
from __future__ import annotations

import re

STATUSES = ("OBSERVED", "INFERRED", "VALIDATED")
ROUTES = ("AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION")
HUMAN_ROUTES = ("HUMAN_DECISION", "HUMAN_VALIDATION")
VALIDATION_METHODS = ("human_review", "runtime_test", "authoritative_source")

# Valores padrão das faixas. policies/escalation.policy.json é a fonte; um teste confere a igualdade.
DEFAULT_LOW_BELOW = 0.5
DEFAULT_HIGH_FROM = 0.8

_AGENT_NAME = re.compile(r"^(ai|agent|tool|bot|llm|system)([:/ _-]|$)", re.IGNORECASE)


def band(confidence: float, low_below: float = DEFAULT_LOW_BELOW, high_from: float = DEFAULT_HIGH_FROM) -> str:
    """Faixa derivada da confiança. A faixa nunca é armazenada: é sempre calculada."""
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise TypeError("confiança deve ser número")
    if not 0 <= confidence <= 1:
        raise ValueError("confiança fora de 0..1")
    if confidence < low_below:
        return "low"
    if confidence >= high_from:
        return "high"
    return "medium"


def looks_like_agent(name: str) -> bool:
    """Heurística de nomeação: identificadores de agente não podem validar nada."""
    return bool(_AGENT_NAME.match(name.strip()))


def check_epistemic(epistemic: dict) -> list[str]:
    """Verificações semânticas de um bloco epistêmico. Complementa o schema `common#/$defs/epistemic`."""
    problems: list[str] = []
    status = epistemic.get("status")
    if status not in STATUSES:
        problems.append(f"status inválido: {status!r}")
    confidence = epistemic.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        problems.append(f"confiança deve ser número entre 0 e 1: {confidence!r}")
    if not epistemic.get("provenance"):
        problems.append("proveniência ausente")
    if status == "INFERRED" and not epistemic.get("basis"):
        problems.append("INFERRED sem basis")
    if status == "VALIDATED":
        validation = epistemic.get("validation") or {}
        who = validation.get("validated_by")
        if not who:
            problems.append("VALIDATED sem validated_by")
        elif looks_like_agent(who):
            problems.append(f"VALIDATED por identificador de agente: {who!r}; deve ser pessoa nomeada")
        if validation.get("method") not in VALIDATION_METHODS:
            problems.append(f"método de validação inválido: {validation.get('method')!r}")
        if epistemic.get("quarantine"):
            problems.append("VALIDATED não pode estar em quarentena")
    return problems
