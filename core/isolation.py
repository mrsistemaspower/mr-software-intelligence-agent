"""Isolamento entre o Agent Core e o conhecimento de software.

Os termos que identificam cada software moram no manifesto do próprio pacote
(`isolation.leak_terms`). Este módulo coleta os termos de todos os pacotes e verifica:

  1. zonas genéricas (núcleo, schemas, skills, adapters, workflows, policies) sem termos de nenhum software;
  2. cada pacote sem termos de outros pacotes;
  3. zonas neutras de provedor sem nomes de provedores de modelo;
  4. o núcleo sem ramificação por `software_id` e sem ids de software como constante;
  5. nenhuma identidade antiga de organização em URLs operacionais.

Adicionar um segundo pacote amplia a verificação automaticamente: nada aqui precisa mudar.
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {".md", ".json", ".py", ".txt", ".yml", ".yaml"}
SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache"}

# Relativos à raiz do repositório.
PACK_ROOTS = ("software-packs", "evaluations/fixtures")
SOFTWARE_NEUTRAL_ZONES = (
    "AGENT.md", "core", "schemas", "skills", "adapters", "workflows", "policies",
    "evaluations/README.md", "evaluations/escalation-cases.json",
    "software-packs/README.md", "software-packs/_template",
)
PROVIDER_NEUTRAL_ZONES = (
    "AGENT.md", "core", "schemas", "skills", "workflows", "policies", "adapters/sources",
    "evaluations/README.md", "evaluations/escalation-cases.json", "software-packs",
)
PROVIDERS_FILE = "adapters/llm/known-providers.json"
OLD_IDENTITY = re.compile(r"mrs{1,2}istemas6", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    line: int
    term: str

    def __str__(self) -> str:
        return f"{self.kind} {self.path}:{self.line} '{self.term}'"


def fold(text: str) -> str:
    """Sem acento e sem diferença de caixa."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def collect_leak_terms(repo: Path) -> dict[str, dict]:
    """software_id -> {'dir': Path, 'terms': [termos dobrados]} para todo pacote com manifesto legível."""
    out: dict[str, dict] = {}
    for rel in PACK_ROOTS:
        base = repo / rel
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            manifest = child / "manifest.json"
            if not manifest.is_file():
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("role") == "template":
                continue
            terms = data.get("isolation", {}).get("leak_terms", {})
            out[data["software_id"]] = {
                "dir": child,
                "terms": sorted({fold(t) for t in terms.get("identity", []) + terms.get("domain", [])}),
            }
    return out


def _text_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    files = []
    for p in sorted(path.rglob("*")):
        if p.is_file() and p.suffix in TEXT_SUFFIXES and not (set(p.relative_to(path).parts[:-1]) & SKIP_DIRS):
            files.append(p)
    return files


def scan_files(files: list[Path], terms: list[str], kind: str, repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            folded = fold(line)
            for term in terms:
                if term in folded:
                    findings.append(Finding(kind, file.relative_to(repo).as_posix(), number, term))
    return findings


def _zone_files(repo: Path, zones: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for zone in zones:
        files.extend(_text_files(repo / zone))
    return files


def scan_software_leaks(repo: Path, packs: dict[str, dict] | None = None) -> list[Finding]:
    packs = collect_leak_terms(repo) if packs is None else packs
    all_terms = sorted({t for p in packs.values() for t in p["terms"]})
    findings = scan_files(_zone_files(repo, SOFTWARE_NEUTRAL_ZONES), all_terms, "SOFTWARE_LEAK_IN_GENERIC", repo)
    for sid, info in packs.items():
        others = sorted({t for other, p in packs.items() if other != sid for t in p["terms"]})
        findings.extend(scan_files(_text_files(info["dir"]), others, "CROSS_PACK_LEAK", repo))
    return findings


def load_provider_terms(repo: Path) -> list[str]:
    """Termos de provedores de modelo. A lista mora em adapters/llm, a única camada que pode nomeá-los."""
    data = json.loads((repo / PROVIDERS_FILE).read_text(encoding="utf-8"))
    return sorted({fold(t) for t in data["terms"]})


def scan_provider_terms(repo: Path) -> list[Finding]:
    return scan_files(_zone_files(repo, PROVIDER_NEUTRAL_ZONES), load_provider_terms(repo), "PROVIDER_IN_NEUTRAL_ZONE", repo)


def scan_old_identity(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for file in _text_files(repo):
        text = file.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            match = OLD_IDENTITY.search(line)
            if match:
                findings.append(Finding("OLD_IDENTITY", file.relative_to(repo).as_posix(), number, match.group(0)))
    return findings


def _terminal_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return ""


def check_core_independence(repo: Path, packs: dict[str, dict] | None = None) -> list[Finding]:
    """O núcleo não pode comparar `software_id` com constante nem conter ids ou termos de software."""
    packs = collect_leak_terms(repo) if packs is None else packs
    forbidden = sorted({fold(sid) for sid in packs} | {t for p in packs.values() for t in p["terms"]})
    findings: list[Finding] = []
    for file in sorted((repo / "core").rglob("*.py")):
        rel = file.relative_to(repo).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                folded = fold(node.value)
                for term in forbidden:
                    if term in folded:
                        findings.append(Finding("SOFTWARE_CONSTANT_IN_CORE", rel, node.lineno, term))
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                names = [_terminal_name(o) for o in operands]
                has_const = any(isinstance(o, ast.Constant) and isinstance(o.value, str) for o in operands)
                if has_const and any("software_id" in n for n in names):
                    findings.append(Finding("SOFTWARE_ID_BRANCH_IN_CORE", rel, node.lineno, "software_id"))
    return findings


def check_all(repo: Path) -> list[Finding]:
    packs = collect_leak_terms(repo)
    return [
        *scan_software_leaks(repo, packs),
        *scan_provider_terms(repo),
        *scan_old_identity(repo),
        *check_core_independence(repo, packs),
    ]
