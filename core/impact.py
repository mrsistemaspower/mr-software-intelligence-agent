"""Inteligência de mudança: dado o que mudou entre versões, calcula o conhecimento afetado.

Só reanalisa o que depende do que mudou. Nada aqui conhece um software específico: o grafo é
construído a partir das referências entre registros de qualquer pacote.
"""
from __future__ import annotations

from collections import deque

from .pack import iter_epistemics, iter_refs

ACTION_RANK = {"NONE": 0, "REVALIDATE": 1, "REANALYZE": 2, "INVALIDATE": 3}


def reference_graph(records: dict[str, dict]) -> dict[str, set[str]]:
    """id -> ids que ele referencia."""
    return {rid: {ref for _, ref in iter_refs(rec)} for rid, rec in records.items()}


def dependents(graph: dict[str, set[str]], seeds: set[str]) -> set[str]:
    """Todos os registros que dependem, direta ou indiretamente, de algum id em `seeds`."""
    reverse: dict[str, set[str]] = {}
    for rid, refs in graph.items():
        for ref in refs:
            reverse.setdefault(ref, set()).add(rid)
    seen: set[str] = set()
    queue = deque(seeds)
    while queue:
        current = queue.popleft()
        for dep in reverse.get(current, ()):
            if dep not in seen and dep not in seeds:
                seen.add(dep)
                queue.append(dep)
    return seen


def _is_validated(record: dict) -> bool:
    return any(ep.get("status") == "VALIDATED" for _, ep in iter_epistemics(record))


def plan_reanalysis(records: dict[str, dict]) -> dict[str, str]:
    """Para cada registro afetado, a ação mais forte exigida por qualquer mudança (`rel:`) do pacote.

    - O assunto da mudança e as referências em `impact.affected_refs` recebem a ação declarada.
    - Quem depende deles recebe, no máximo, REVALIDATE.
    - Registro VALIDATED afetado nunca fica abaixo de REVALIDATE: a validação vale para a versão antiga.
    - Registros de mudança (`rel:`) não entram no plano.
    """
    graph = reference_graph(records)
    plan: dict[str, str] = {}

    def assign(rid: str, action: str) -> None:
        if rid.startswith("rel:") or rid not in records:
            return
        if _is_validated(records[rid]) and ACTION_RANK[action] < ACTION_RANK["REVALIDATE"]:
            action = "REVALIDATE"
        if ACTION_RANK[action] > ACTION_RANK.get(plan.get(rid, "NONE"), 0):
            plan[rid] = action

    for rid, rec in records.items():
        if not rid.startswith("rel:"):
            continue
        action = rec["impact"]["action"]
        if action == "NONE":
            continue
        direct = set(rec["impact"]["affected_refs"])
        subject = rec["subject"].get("ref")
        if subject:
            direct.add(subject)
        direct = {d for d in direct if d in records}
        for rid_direct in direct:
            assign(rid_direct, action)
        for rid_dep in dependents(graph, direct):
            assign(rid_dep, "REVALIDATE")
    return dict(sorted(plan.items()))
