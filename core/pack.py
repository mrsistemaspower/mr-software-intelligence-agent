"""Carga e validação de Software Knowledge Packs.

Valida o que os schemas isolados não conseguem: referências entre registros, unicidade de ids,
hash fixado na proveniência igual ao da fonte, contradições abertas com posições em quarentena,
validações ligadas a revisões respondidas por pessoa e higiene de caminhos nos arquivos públicos.

Códigos de problema: P001 leitura, P002 a P009 estrutura do pacote, P010 a P013 registros,
P020 a P036 invariantes entre registros, P040 semântica epistêmica.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .epistemics import HUMAN_ROUTES, check_epistemic, looks_like_agent
from .schema_lite import Registry, Validator

# coleção -> (schema, prefixo de id)
COLLECTIONS: dict[str, tuple[str, str]] = {
    "sources": ("source", "src"),
    "evidence": ("evidence", "evd"),
    "facts": ("fact", "fct"),
    "screens": ("screen", "scr"),
    "interactions": ("interaction", "int"),
    "lookups": ("lookup", "lku"),
    "workflows": ("workflow", "wfl"),
    "business-rules": ("business-rule", "rul"),
    "contradictions": ("contradiction", "ctr"),
    "reviews": ("human-review", "rev"),
    "releases": ("release-change", "rel"),
}

ID_RE = re.compile(r"^(src|evd|fct|scr|int|lku|wfl|rul|ctr|rev|rel):[a-z0-9][a-z0-9._-]*$")
# Letra de unidade, caminho UNC ou raiz típica de sistema. Aplicado a valores já decodificados.
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|^\\\\|(?<![A-Za-z0-9.])/(?:home|Users|usr|var|mnt|opt|etc)/")


@dataclass(frozen=True)
class Issue:
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.where}: {self.message}"


@dataclass
class Pack:
    root: Path
    manifest: dict
    records: dict[str, dict]
    owner: dict[str, str]  # id -> coleção


# ------------------------------------------------------------------ utilidades
def iter_strings(node: object, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from iter_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from iter_strings(value, f"{path}[{i}]")


def iter_refs(record: dict) -> Iterator[tuple[str, str]]:
    """Todo valor string que é, por inteiro, um id de registro. O `id` do próprio registro é ignorado."""
    for path, text in iter_strings(record):
        if path != "$.id" and ID_RE.match(text):
            yield path, text


def iter_epistemics(record: dict) -> Iterator[tuple[str, dict]]:
    if isinstance(record.get("epistemic"), dict):
        yield "epistemic", record["epistemic"]
    for element in record.get("elements") or []:
        if isinstance(element, dict) and isinstance(element.get("epistemic"), dict):
            yield f"elements[{element.get('key')}].epistemic", element["epistemic"]


def discover_packs(*roots: Path | str) -> list[Path]:
    """Diretórios (filhos diretos de cada raiz) que contêm manifest.json."""
    found: list[Path] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "manifest.json").is_file():
                found.append(child)
    return found


def _read_json(path: Path, add: Callable[[str, str, str], None]) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        add("P001", path.name, "arquivo ausente")
    except (OSError, ValueError) as exc:
        add("P001", path.name, f"JSON ilegível: {exc}")
    return None


# ------------------------------------------------------------------ carga
def load_pack(root: Path | str) -> Pack:
    """Carrega um pacote já validado. Levanta ValueError se houver problema estrutural."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    records: dict[str, dict] = {}
    owner: dict[str, str] = {}
    for name in manifest.get("collections", []):
        data = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
        for rec in data["records"]:
            if rec["id"] in records:
                raise ValueError(f"id duplicado: {rec['id']}")
            records[rec["id"]] = rec
            owner[rec["id"]] = name
    return Pack(root=root, manifest=manifest, records=records, owner=owner)


# ------------------------------------------------------------------ validação
def validate_pack(root: Path | str, registry: Registry | None = None) -> list[Issue]:
    root = Path(root)
    validator = Validator(registry or Registry())
    issues: list[Issue] = []

    def add(code: str, where: str, message: str) -> None:
        issues.append(Issue(code, where, message))

    manifest = _read_json(root / "manifest.json", add)
    if not isinstance(manifest, dict):
        return issues
    for err in validator.validate(manifest, "software-pack-manifest"):
        add("P002", "manifest.json", err)
    if issues:
        return issues

    sid = manifest["software_id"]
    if manifest["role"] != "template" and root.name != sid:
        add("P003", "manifest.json", f"software_id '{sid}' difere do nome do diretório '{root.name}'")

    _check_metadata(root, manifest, validator, add)
    records, owner = _load_records(root, manifest, validator, add)
    if issues:
        return issues
    _check_records(manifest, records, owner, add)
    return issues


def _check_metadata(root: Path, manifest: dict, validator: Validator, add: Callable[[str, str, str], None]) -> None:
    sid = manifest["software_id"]
    public: list[tuple[str, object]] = [("manifest.json", manifest)]

    software = _read_json(root / "software.json", add)
    if isinstance(software, dict):
        for err in validator.validate(software, "software"):
            add("P004", "software.json", err)
        if software.get("software_id") != sid:
            add("P004", "software.json", "software_id difere do manifesto")
        if software.get("display_name") != manifest["display_name"]:
            add("P004", "software.json", "display_name difere do manifesto")
        public.append(("software.json", software))

    integration = manifest["integration"]
    if integration.get("reference_file"):
        ref_name = integration["reference_file"]
        ref = _read_json(root / ref_name, add)
        if isinstance(ref, dict):
            for err in validator.validate(ref, "integration-reference"):
                add("P005", ref_name, err)
            if ref.get("software_id") != sid:
                add("P005", ref_name, "software_id difere do manifesto")
            public.append((ref_name, ref))

    for name, doc in public:
        for path, text in iter_strings(doc):
            if ABS_PATH_RE.search(text):
                add("P006", name, f"caminho absoluto em arquivo público ({path})")


def _load_records(
    root: Path, manifest: dict, validator: Validator, add: Callable[[str, str, str], None]
) -> tuple[dict[str, dict], dict[str, str]]:
    declared = list(manifest["collections"])
    present = {name for name in COLLECTIONS if (root / f"{name}.json").exists()}
    for name in declared:
        if name not in present:
            add("P007", f"{name}.json", "coleção declarada no manifesto e ausente")
    for name in sorted(present - set(declared)):
        add("P008", f"{name}.json", "coleção presente e não declarada no manifesto")

    records: dict[str, dict] = {}
    owner: dict[str, str] = {}
    for name in declared:
        if name not in present:
            continue
        schema_name, prefix = COLLECTIONS[name]
        data = _read_json(root / f"{name}.json", add)
        if data is None:
            continue
        if (
            not isinstance(data, dict)
            or set(data) != {"collection", "records"}
            or data["collection"] != name
            or not isinstance(data["records"], list)
        ):
            add("P009", f"{name}.json", "container inválido: esperado {collection, records[]}")
            continue
        for index, rec in enumerate(data["records"]):
            where = f"{name}.json#{index}"
            errors = validator.validate(rec, schema_name)
            for err in errors:
                add("P010", where, err)
            rid = rec.get("id") if isinstance(rec, dict) else None
            if errors or not isinstance(rid, str):
                continue
            if not rid.startswith(prefix + ":"):
                add("P011", where, f"id '{rid}' não usa o prefixo '{prefix}:' da coleção")
            if rid in records:
                add("P012", where, f"id duplicado '{rid}' (também em {owner[rid]})")
                continue
            records[rid] = rec
            owner[rid] = name
    return records, owner


def _check_records(
    manifest: dict, records: dict[str, dict], owner: dict[str, str], add: Callable[[str, str, str], None]
) -> None:
    workspace = manifest["external_workspace"]["workspace_id"]
    by_kind = lambda name: [r for r in records.values() if owner[r["id"]] == name]  # noqa: E731

    # P020 referências
    for rid, rec in records.items():
        for path, ref in iter_refs(rec):
            if ref not in records:
                add("P020", rid, f"referência inexistente '{ref}' em {path}")

    # P013 workspace das fontes
    for src in by_kind("sources"):
        if src["locator"]["workspace"] != workspace:
            add("P013", src["id"], f"workspace '{src['locator']['workspace']}' não é o declarado no manifesto ('{workspace}')")

    # P021 a P023 proveniência; P040 semântica epistêmica
    for rid, rec in records.items():
        for label, ep in iter_epistemics(rec):
            for message in check_epistemic(ep):
                add("P040", rid, f"{label}: {message}")
            for entry in ep["provenance"]:
                src = records.get(entry["source_id"])
                if src is None:
                    continue
                if "evidence_id" in entry:
                    ev = records.get(entry["evidence_id"])
                    if ev is not None and ev["source_id"] != entry["source_id"]:
                        add("P021", rid, f"{label}: evidência '{ev['id']}' pertence a '{ev['source_id']}', não a '{entry['source_id']}'")
                if "source_sha256" in entry and entry["source_sha256"] != src.get("sha256"):
                    add("P022", rid, f"{label}: hash fixado não confere com o da fonte '{src['id']}' (fonte alterada ou hash ausente)")

    # P023 a P026 telas, interações, lookups, workflows
    elements: dict[str, set[str]] = {}
    for scr in by_kind("screens"):
        keys = [el["key"] for el in scr.get("elements", [])]
        if len(keys) != len(set(keys)):
            add("P023", scr["id"], "chaves de elemento repetidas")
        elements[scr["id"]] = set(keys)
        if scr["grouping"]["status"] == "UNRESOLVED" and len(scr["captures"]) > 1 and scr["epistemic"]["status"] == "VALIDATED":
            add("P035", scr["id"], "tela com agrupamento UNRESOLVED não pode ser VALIDATED")
    for it in by_kind("interactions"):
        if it["trigger"]["element_key"] not in elements.get(it["screen_id"], set()):
            add("P024", it["id"], f"elemento '{it['trigger']['element_key']}' não existe na tela '{it['screen_id']}'")
    for lk in by_kind("lookups"):
        if lk["from"]["element_key"] not in elements.get(lk["from"]["screen_id"], set()):
            add("P024", lk["id"], f"elemento '{lk['from']['element_key']}' não existe na tela '{lk['from']['screen_id']}'")
    for wf in by_kind("workflows"):
        orders = [s["order"] for s in wf["steps"]]
        if len(orders) != len(set(orders)):
            add("P026", wf["id"], "ordens de passo repetidas")

    # P027 a P030 contradições e quarentena
    quarantined_by: dict[str, set[str]] = {}
    for rid, rec in records.items():
        for _, ep in iter_epistemics(rec):
            q = ep.get("quarantine")
            if q and q.get("ref"):
                quarantined_by.setdefault(q["ref"], set()).add(rid)
    for ctr in by_kind("contradictions"):
        refs = {p["ref"] for p in ctr["positions"]}
        if ctr["status"] == "OPEN":
            for ref in sorted(refs):
                target = records.get(ref)
                if target is None or ref not in quarantined_by.get(ctr["id"], set()):
                    add("P028", ctr["id"], f"posição '{ref}' deve estar em quarentena apontando para esta contradição")
        else:
            chosen = ctr["resolution"].get("chosen_ref")
            if chosen and chosen not in refs:
                add("P027", ctr["id"], f"chosen_ref '{chosen}' não é uma das posições")
            if quarantined_by.get(ctr["id"]):
                add("P029", ctr["id"], "contradição resolvida mantém registros em quarentena: " + ", ".join(sorted(quarantined_by[ctr["id"]])))

    # P031 a P036 revisões
    reviews = {r["id"]: r for r in by_kind("reviews")}
    for rev in reviews.values():
        if rev["route"] == "HUMAN_VALIDATION":
            hyp = records.get(rev["hypothesis_ref"])
            if hyp is not None and rev["state"] == "OPEN" and hyp.get("epistemic", {}).get("status") != "INFERRED":
                add("P031", rev["id"], f"hipótese '{rev['hypothesis_ref']}' deve ser INFERRED enquanto a revisão está aberta")
        answer = rev.get("answer")
        if answer and answer["actor"] == "person" and looks_like_agent(answer["answered_by"]):
            add("P036", rev["id"], f"answered_by '{answer['answered_by']}' parece identificador de agente")
        if answer:
            for effect in answer.get("effects", []):
                if effect["effect"] == "validates":
                    target = records.get(effect["ref"])
                    validation = (target or {}).get("epistemic", {}).get("validation") or {}
                    if target is not None and validation.get("review_id") != rev["id"]:
                        add("P033", rev["id"], f"'{effect['ref']}' é validado por esta revisão, mas não aponta para ela em validation.review_id")
    for rid, rec in records.items():
        for label, ep in iter_epistemics(rec):
            validation = ep.get("validation") or {}
            review_id = validation.get("review_id")
            if not review_id:
                continue
            rev = reviews.get(review_id)
            if rev is None:
                continue  # P020 já cobre inexistência
            if rev["route"] not in HUMAN_ROUTES or rev["state"] != "ANSWERED":
                add("P032", rid, f"{label}: revisão '{review_id}' deve ser humana e estar ANSWERED")
            elif rev["answer"]["answered_by"] != validation["validated_by"]:
                add("P032", rid, f"{label}: validated_by difere de quem respondeu a revisão '{review_id}'")

    # P034 releases
    for rel in by_kind("releases"):
        if rel["from_version"]["label"] == rel["to_version"]["label"]:
            add("P034", rel["id"], "from_version e to_version iguais")
