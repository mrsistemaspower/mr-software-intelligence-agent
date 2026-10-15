"""Extração 2: acoplamento zero, determinismo entre processos, compatibilidade, Git opcional, camadas e rastreabilidade."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import REPO, TempDirCase, load_json
from core import __version__, isolation
from core.serialization import canonical_pretty_bytes

NEW_MODULES = ["stage_graph", "stage_state", "checkpoint", "protection", "run_config", "run_state", "resume", "orchestrator", "gitstate"]
NEW_SCHEMAS = ["stage-graph", "stage-checkpoint", "run-config", "run-state"]
NEW_FILES = [REPO / "core" / f"{name}.py" for name in NEW_MODULES]
SCHEMA_FILES = [REPO / "schemas" / f"{name}.schema.json" for name in NEW_SCHEMAS]
GENERIC_FILES = NEW_FILES + SCHEMA_FILES + [REPO / "schemas" / "agent-workflow.schema.json"]
INTENDED_MINIMUM = (3, 10)
SYNTAX_AUDITED_DOWN_TO = (3, 9)

# Quem pode importar quem dentro do núcleo novo. Garante camadas sem ciclo e que as primitivas da Extração 1 são reusadas.
ALLOWED_INTERNAL = {
    "stage_graph": {"hashing", "paths", "schema_lite"},
    "stage_state": {"paths", "stage_graph"},
    "protection": {"integrity", "paths", "snapshot", "stage_graph"},
    "checkpoint": {"atomic", "hashing", "identity", "integrity", "paths", "schema_lite", "serialization", "snapshot", "stage_graph"},
    "run_config": {"hashing", "paths", "protection", "schema_lite", "serialization", "snapshot", "stage_graph"},
    "run_state": {"__version__", "atomic", "hashing", "paths", "protection", "schema_lite", "serialization", "snapshot", "stage_graph", "stage_state"},
    "resume": {"checkpoint", "run_config", "run_state", "stage_graph", "stage_state"},
    "orchestrator": {"atomic", "checkpoint", "hashing", "paths", "protection", "resume", "run_config", "run_state", "serialization", "snapshot", "stage_graph", "stage_state"},
    "gitstate": {"paths"},
}


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def tree(path: Path) -> ast.AST:
    return ast.parse(source(path), filename=path.name)


class NoSyscomCouplingInTheGenericCore(unittest.TestCase):
    """O núcleo de orquestração não conhece o projeto de referência, suas fases, telas, datas ou caminhos."""

    FORBIDDEN = re.compile(
        r"\b(syscom|mr-syscom|mr_syscom|k3|k4|k3_4|k41|k42|cadastro|nota\s+fiscal|firebird|dokuwiki|dolphins|pilot|mr-ai)\b|\bscr-|k4[._]\d",
        re.IGNORECASE,
    )
    FIXED_DATE = re.compile(r"\b(19|20)\d\d-\d\d-\d\d\b")
    DRIVE_PATH = re.compile(r"[A-Za-z]:\\[A-Za-z]")
    HARDCODED_COUNT = re.compile(r"\b(total|expected|screens?)\s*[:=]\s*\d{2,}\b", re.IGNORECASE)

    def test_the_new_files_exist(self) -> None:
        for path in GENERIC_FILES:
            self.assertTrue(path.is_file(), path.name)

    def test_zero_reference_project_identifiers_in_any_generic_file(self) -> None:
        hits = []
        for path in GENERIC_FILES:
            for number, line in enumerate(source(path).splitlines(), start=1):
                match = self.FORBIDDEN.search(line)
                if match:
                    hits.append(f"{path.name}:{number}: {match.group(0)}")
        self.assertEqual(hits, [], "acoplamento ao projeto de referência no núcleo genérico")

    def test_the_term_scan_has_teeth(self) -> None:
        for sample in ("syscom", "SYSCOM", "mr-syscom", "k3", "K4", "k4.1", "k4_2", "Cadastro", "cadastro de cor", "Nota Fiscal", "NOTA   FISCAL",
                       "scr-01", "pilot", "Pilot", "data/pilot/k3", "Firebird", "C:\\MR-AI", "DokuWiki"):
            self.assertIsNotNone(self.FORBIDDEN.search(f"x {sample} y"), sample)
        for innocent in ("hashing", "backup4", "block3", "sha256", "description-", "pilotless", "escr-", "stage", "workflow"):
            self.assertIsNone(self.FORBIDDEN.search(f"x {innocent} y"), innocent)

    def test_no_fixed_date_no_drive_path_no_hardcoded_totals(self) -> None:
        for path in GENERIC_FILES:
            text = source(path)
            with self.subTest(file=path.name):
                self.assertIsNone(self.FIXED_DATE.search(text))
                self.assertIsNone(self.DRIVE_PATH.search(text))
                self.assertIsNone(self.HARDCODED_COUNT.search(text))

    def test_every_leak_term_of_every_pack_is_absent_from_the_generic_files(self) -> None:
        terms = sorted({t for info in isolation.collect_leak_terms(REPO).values() for t in info["terms"]})
        self.assertGreaterEqual(len(terms), 5)
        self.assertEqual([str(f) for f in isolation.scan_files(GENERIC_FILES, terms, "LEAK", REPO)], [])

    def test_no_model_provider_term_in_the_generic_files(self) -> None:
        self.assertEqual([str(f) for f in isolation.scan_files(GENERIC_FILES, isolation.load_provider_terms(REPO), "PROVIDER", REPO)], [])

    def test_the_whole_repository_isolation_check_still_passes(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_all(REPO)], [])

    def test_no_branch_on_software_identity_in_the_core(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_core_independence(REPO)], [])

    def test_no_stage_name_or_count_is_special_to_the_core(self) -> None:
        # Nomes de exemplo que aparecem em documentação não podem virar constantes de código.
        reserved = {"inventory", "discover", "analyze", "validate", "document", "documentation", "ui-analysis", "database-analysis"}
        for path in NEW_FILES:
            constants = {n.value for n in ast.walk(tree(path)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            with self.subTest(file=path.name):
                self.assertEqual(constants & reserved, set())

    def test_windows_specific_logic_stays_in_paths_py(self) -> None:
        pattern = re.compile(r"""["']nt["']|win32|msvcrt|winreg|ntpath|\\\\\\\\\?""")
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(pattern.findall(source(path)), [])


class DeterministicInputsOnly(unittest.TestCase):
    """Nada de relógio, ambiente, acaso ou rede: o que entra no resultado é o que foi passado como argumento."""

    ALLOWED_IMPORTS = {"__future__", "copy", "dataclasses", "enum", "functools", "heapq", "os", "pathlib", "re", "typing"}
    BANNED_ATTRIBUTES = {
        "environ", "getenv", "getlogin", "gethostname", "node", "now", "utcnow", "today", "uuid1", "uuid4", "time", "time_ns",
        "monotonic", "perf_counter", "getpid", "getuid", "getcwd", "random", "randint", "urandom", "system", "popen", "Popen",
    }

    def imported(self, path: Path) -> set:
        names: set = set()
        for node in ast.walk(tree(path)):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_only_the_standard_library_subset_is_imported(self) -> None:
        for path in NEW_FILES:
            allowed = self.ALLOWED_IMPORTS | ({"subprocess"} if path.name == "gitstate.py" else set())
            with self.subTest(file=path.name):
                self.assertLessEqual(self.imported(path), allowed)

    def test_no_primitive_of_extraction_1_is_reimplemented(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertFalse(self.imported(path) & {"hashlib", "json", "secrets", "tempfile", "shutil"})

    def test_no_ambient_input_is_ever_read(self) -> None:
        for path in NEW_FILES:
            attributes = {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            with self.subTest(file=path.name):
                self.assertEqual(attributes & self.BANNED_ATTRIBUTES, set())

    def test_the_ambient_scan_has_teeth(self) -> None:
        sample = ast.parse("import datetime, os\nx = datetime.datetime.now()\ny = os.environ['A']\nz = os.getcwd()\n")
        attributes = {n.attr for n in ast.walk(sample) if isinstance(n, ast.Attribute)}
        self.assertEqual(attributes & self.BANNED_ATTRIBUTES, {"now", "environ", "getcwd"})

    def test_no_model_sdk_or_network_import(self) -> None:
        banned = {"anthropic", "openai", "requests", "urllib", "http", "socket", "ssl", "asyncio", "threading", "multiprocessing"}
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(self.imported(path) & banned, set())

    def test_the_clock_is_injected_not_read(self) -> None:
        text = source(REPO / "core" / "orchestrator.py")
        self.assertIn("now: Callable[[], str] | None = None", text)
        self.assertNotIn("import datetime", text)
        self.assertNotIn("import time", text)


class LayeringAndReuse(unittest.TestCase):
    def internal_imports(self, path: Path) -> set:
        found: set = set()
        for node in ast.walk(tree(path)):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    found.add(node.module)
                else:
                    found.update(alias.name for alias in node.names)
        return found

    def test_every_new_module_is_in_the_layering_table(self) -> None:
        self.assertEqual(set(ALLOWED_INTERNAL), set(NEW_MODULES))

    def test_internal_imports_follow_the_layering(self) -> None:
        for name in NEW_MODULES:
            with self.subTest(name):
                self.assertLessEqual(self.internal_imports(REPO / "core" / f"{name}.py"), ALLOWED_INTERNAL[name])

    def test_the_layering_has_no_cycle(self) -> None:
        graph = {name: {dep for dep in deps if dep in ALLOWED_INTERNAL} for name, deps in ALLOWED_INTERNAL.items()}
        done: set = set()
        while len(done) < len(graph):
            ready = [name for name in graph if name not in done and graph[name] <= done]
            self.assertTrue(ready, "ciclo entre módulos")
            done.update(ready)

    def test_the_primitives_of_extraction_1_are_reused_where_they_belong(self) -> None:
        expectations = {
            "checkpoint": {"atomic", "hashing", "identity", "integrity", "serialization"},
            "protection": {"integrity", "snapshot"},
            "run_state": {"atomic", "hashing", "serialization"},
            "orchestrator": {"atomic", "hashing", "serialization", "snapshot"},
            "run_config": {"hashing", "serialization", "snapshot"},
        }
        for name, needed in expectations.items():
            with self.subTest(name):
                self.assertLessEqual(needed, self.internal_imports(REPO / "core" / f"{name}.py"))

    def test_schema_registry_knows_the_new_contracts(self) -> None:
        from core.schema_lite import Registry

        registry = Registry()
        for name in NEW_SCHEMAS:
            self.assertIn(f"urn:mr-software-intelligence:schema:{name}:1", registry.by_id)
        self.assertEqual(registry.check_all(), [])


class RuntimeCompatibility(unittest.TestCase):
    """Mínimo pretendido 3.10; sintaxe auditada até 3.9. Só o interpretador de hoje executa."""

    def test_syntax_is_valid_for_the_audited_floor(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                ast.parse(source(path), filename=path.name, feature_version=SYNTAX_AUDITED_DOWN_TO)

    def test_the_floor_check_has_teeth(self) -> None:
        with self.assertRaises(SyntaxError):
            ast.parse("match x:\n    case 1:\n        pass\n", feature_version=SYNTAX_AUDITED_DOWN_TO)

    def test_no_api_or_keyword_newer_than_the_audited_floor(self) -> None:
        newer = {"bit_count", "pairwise", "batched", "file_digest", "StrEnum", "ExceptionGroup", "TaskGroup", "tomllib", "Self", "override",
                 "TypeAlias", "ParamSpec", "Required", "NotRequired", "LiteralString", "Never", "assert_never", "removeprefix_all"}
        newer_keywords = {"strict", "slots", "kw_only", "match_args"}
        for path in NEW_FILES:
            module = tree(path)
            names = {n.attr for n in ast.walk(module) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(module) if isinstance(n, ast.Name)}
            keywords = {k.arg for n in ast.walk(module) if isinstance(n, ast.Call) for k in n.keywords}
            with self.subTest(file=path.name):
                self.assertEqual(names & newer, set())
                self.assertEqual(keywords & newer_keywords, set())

    def test_no_release_candidate_only_feature_is_used(self) -> None:
        # Nada que só exista em 3.15: o conjunto de módulos importados e atributos acima é todo de 3.9/3.10.
        for path in NEW_FILES:
            self.assertNotIn("sys.version_info", source(path))

    def test_annotations_are_lazy_so_modern_union_syntax_never_runs_on_older_pythons(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertIn("from __future__ import annotations", source(path))

    def test_no_runtime_use_of_union_operator_on_types(self) -> None:
        # `X | Y` só é seguro em anotações (lazy). Em isinstance() ou atribuição quebraria no 3.9, que o código ainda pretende tolerar na sintaxe.
        for path in NEW_FILES:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "isinstance" and len(node.args) == 2:
                    with self.subTest(file=path.name, line=node.lineno):
                        self.assertNotIsInstance(node.args[1], ast.BinOp)

    def test_declared_versions(self) -> None:
        self.assertLessEqual(SYNTAX_AUDITED_DOWN_TO, INTENDED_MINIMUM)
        self.assertGreaterEqual(sys.version_info[:2], INTENDED_MINIMUM)

    def test_core_version_was_bumped_for_the_new_capability(self) -> None:
        self.assertEqual(__version__, "1.2.0")


class ImportHasNoSideEffects(TempDirCase):
    def test_importing_every_module_prints_and_writes_nothing(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        code = "import " + ", ".join(f"core.{name}" for name in NEW_MODULES)
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tmp, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_every_new_module_documents_itself(self) -> None:
        for path in NEW_FILES:
            doc = ast.get_docstring(tree(path))
            with self.subTest(file=path.name):
                self.assertTrue(doc and len(doc) > 120)


class GitIsOptional(TempDirCase):
    """Git é uma capacidade do ambiente: o núcleo não o exige, não o importa e roda sem ele."""

    def test_only_the_adapter_touches_subprocess_and_nobody_imports_the_adapter(self) -> None:
        for path in sorted((REPO / "core").glob("*.py")):
            module = tree(path)
            imports = set()
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        imports.add(node.module.split(".")[0])
                    elif node.level == 1:
                        imports.add(node.module or "")
                        imports.update(alias.name for alias in node.names)
            with self.subTest(file=path.name):
                if path.name == "gitstate.py":
                    self.assertIn("subprocess", imports)
                else:
                    self.assertNotIn("subprocess", imports)
                    self.assertNotIn("gitstate", imports)

    def test_the_orchestrator_accepts_a_generic_probe_not_a_git_object(self) -> None:
        text = source(REPO / "core" / "orchestrator.py")
        code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        self.assertNotIn("git_state", "\n".join(code_lines))
        self.assertIn("class EnvironmentProbe(Protocol)", text)

    def test_a_full_workflow_runs_with_subprocess_and_the_adapter_made_unimportable(self) -> None:
        script = (
            "import sys, tempfile, pathlib\n"
            "sys.modules['subprocess'] = None\n"
            "sys.modules['core.gitstate'] = None\n"
            "from tests.wf_helpers import Scenario, linear_specs, LINEAR_INPUTS\n"
            "with tempfile.TemporaryDirectory() as t:\n"
            "    s = Scenario(pathlib.Path(t), linear_specs(), LINEAR_INPUTS)\n"
            "    first = s.run()\n"
            "    second = s.run()\n"
            "    print(first.status.value, len(second.skipped))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1", "PATH": ""}
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=self.tmp, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout.decode().split(), ["COMPLETED", "5"])


SCRIPT = r'''
import hashlib, json, pathlib, sys, tempfile
from tests.wf_helpers import Scenario, linear_specs, branched_specs, LINEAR_INPUTS, BRANCHED_INPUTS

def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

def observe(specs, inputs, tag):
    with tempfile.TemporaryDirectory() as t:
        s = Scenario(pathlib.Path(t), specs, inputs, tag=tag)
        s.run()
        s.write_input(sorted(inputs)[-1], "changed\n")
        s.run()
        return {"calls": s.calls, "state": digest(s.state_file),
                "checkpoints": {n: digest(s.checkpoint_file(n)) for n in s.graph.order},
                "outputs": {n: digest(s.output_file(n)) for n in s.graph.order}}

print(json.dumps({"linear": observe(linear_specs(), LINEAR_INPUTS, "ws-a"),
                  "branched": observe(branched_specs(), BRANCHED_INPUTS, "deep/er/ws-b")}, sort_keys=True))
'''


class DeterministicAcrossProcesses(TempDirCase):
    """Outro processo, outro diretório, outra semente de hash, outra codificação e outro fuso: mesmo estado, byte a byte."""

    def run_script(self, **overrides: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONHASHSEED", "PYTHONUTF8", "PYTHONIOENCODING"}}
        env.update(PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
        env.update(overrides)
        workdir = self.tmp / f"cwd-{len(list(self.tmp.iterdir()))}"
        workdir.mkdir()
        result = subprocess.run([sys.executable, "-B", "-c", SCRIPT], cwd=workdir, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def test_hostile_environments_agree_with_each_other_and_with_this_process(self) -> None:
        namespace: dict = {}
        exec(compile(SCRIPT.replace("print(json.dumps", "RESULT = (json.dumps"), "observe", "exec"), namespace)
        reference = json.loads(namespace["RESULT"])
        for overrides in (
            {"PYTHONHASHSEED": "1"},
            {"PYTHONHASHSEED": "random", "PYTHONUTF8": "1", "LANG": "C", "LC_ALL": "C", "TZ": "Pacific/Kiritimati"},
            {"PYTHONHASHSEED": "31337", "PYTHONIOENCODING": "ascii", "COMPUTERNAME": "OUTRA-MAQUINA", "USERNAME": "outra-pessoa"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.run_script(**overrides), reference)

    def test_the_observation_has_the_expected_shape(self) -> None:
        observed = self.run_script(PYTHONHASHSEED="0")
        self.assertEqual(observed["linear"]["calls"][-3:], ["analyze", "validate", "document"][-3:])
        self.assertEqual(set(observed["linear"]["checkpoints"]), {"inventory", "discover", "analyze", "validate", "document"})
        self.assertRegex(observed["linear"]["state"], r"^[0-9a-f]{64}$")


TRACE = REPO / "docs" / "extraction" / "EXTRACTION_002.json"
CLASSES = {"GENERIC_BEHAVIOR", "SYSCOM_COUPLING", "DATASET_SPECIFIC", "HISTORICAL_ACCIDENT"}
CAPABILITY_FIELDS = {"id", "capability", "source_reference", "source_classification", "generic_invariant", "agent_destination", "rejected_coupling", "tests"}
STUDIED = {"tools/batch/batch_run.py", "tools/batch/k4_hardening_lib.py", "tools/batch/k4_stage_state.py", "tools/batch/k4_execution_record.py"}


class TraceabilityArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.trace = load_json(TRACE)

    def test_header(self) -> None:
        trace = self.trace
        self.assertEqual(trace["extraction"], "EXTRACTION_002")
        self.assertEqual(trace["base_commit"], "cde63f91678971e7a560b2cf0f38e4a938f01776")
        reference = trace["reference"]
        self.assertEqual(reference["access"], "read-only")
        self.assertEqual(reference["commit"], "4c652a38c37d1c492837e82a0cef06de59de2a6c")
        self.assertEqual({item["path"] for item in reference["sources"]}, STUDIED)
        for item in reference["sources"]:
            self.assertRegex(item["git_blob"], r"^[0-9a-f]{40}$")

    def test_every_capability_has_every_required_field(self) -> None:
        self.assertGreaterEqual(len(self.trace["capabilities"]), 12)
        for capability in self.trace["capabilities"]:
            with self.subTest(capability["id"]):
                self.assertTrue(CAPABILITY_FIELDS <= set(capability), CAPABILITY_FIELDS - set(capability))
                for field in ("capability", "generic_invariant"):
                    self.assertTrue(capability[field].strip())
                self.assertTrue(capability["rejected_coupling"], "toda extração rejeita algum acoplamento")
                self.assertTrue(capability["tests"])
                self.assertTrue(capability["source_classification"])
                self.assertTrue(set(capability["source_classification"]) <= CLASSES)
                self.assertTrue(capability["source_reference"]["file"] and capability["source_reference"]["symbols"])

    def test_ids_are_unique_and_ordered(self) -> None:
        ids = [c["id"] for c in self.trace["capabilities"]]
        self.assertEqual(ids, sorted(set(ids)))
        for identifier in ids:
            self.assertRegex(identifier, r"^EX2-\d\d$")

    def test_source_references_point_at_the_studied_files(self) -> None:
        references = []
        for capability in self.trace["capabilities"]:
            references.append(capability["source_reference"])
            references.extend(capability["source_reference"].get("also", []))
        for item in self.trace["deferred"] + self.trace["rejected"]:
            references.append(item["source_reference"])
        for reference in references:
            with self.subTest(reference["file"], symbols=reference["symbols"]):
                self.assertIn(reference["file"], STUDIED)
                self.assertTrue(reference["symbols"])

    def test_destinations_exist_and_define_what_they_claim(self) -> None:
        for capability in self.trace["capabilities"]:
            for destination in capability["agent_destination"]:
                path = REPO / destination["file"]
                with self.subTest(capability["id"], destination=destination["file"]):
                    self.assertTrue(path.is_file())
                    if path.suffix == ".py":
                        module = tree(path)
                        defined = {n.name for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
                        defined |= {t.id for n in ast.walk(module) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
                        defined |= {n.target.id for n in ast.walk(module) if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
                        for symbol in destination["symbols"]:
                            self.assertIn(symbol, defined)
                    else:
                        self.assertEqual(destination["symbols"], [])

    def test_every_new_module_and_schema_is_traced_to_a_capability(self) -> None:
        destinations = {d["file"] for c in self.trace["capabilities"] for d in c["agent_destination"]}
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", destinations)
        for name in NEW_SCHEMAS:
            self.assertIn(f"schemas/{name}.schema.json", destinations)
        self.assertIn("schemas/agent-workflow.schema.json", destinations)

    def test_referenced_tests_exist(self) -> None:
        for capability in self.trace["capabilities"]:
            for reference in capability["tests"]:
                file, _, cls = reference.partition("::")
                path = REPO / file
                with self.subTest(reference):
                    self.assertTrue(path.is_file(), file)
                    self.assertIn(f"class {cls}(", source(path), reference)

    def test_deferred_and_rejected_items_say_why(self) -> None:
        self.assertTrue(self.trace["deferred"])
        for item in self.trace["deferred"]:
            self.assertTrue(item["reason"].strip() and item["planned_destination"].strip(), item)
        self.assertTrue(self.trace["rejected"])
        for item in self.trace["rejected"]:
            self.assertTrue(item["reason"].strip() and item["classification"] in CLASSES, item)

    def test_not_extracted_list_matches_the_scope_boundary(self) -> None:
        names = " ".join(self.trace["not_extracted"]).lower()
        for word in ("ai_recheck", "modelo de linguagem", "revisão humana", "perguntas", "lookup", "analisador de telas", "runtime", "dokuwiki", "observador", "interface web"):
            self.assertIn(word, names, word)

    def test_the_artifact_is_portable(self) -> None:
        text = TRACE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\", text))
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9.])/(home|Users|mnt|var|usr|opt)/", text))
        self.assertNotIn(REPO.parent.name, text)
        from core.paths import find_local_paths

        self.assertEqual(find_local_paths(self.trace), [])

    def test_the_artifact_is_canonical_pretty_json(self) -> None:
        raw = TRACE.read_bytes()
        self.assertEqual(raw, canonical_pretty_bytes(json.loads(raw.decode("utf-8"))))


class DocumentationOfTheExtraction(unittest.TestCase):
    DOC = REPO / "docs" / "AGENT_EXTRACTION_2.md"
    SECTIONS = [
        "Fontes estudadas", "Invariantes genéricos extraídos", "Acoplamento do SYSCOM rejeitado", "Grafo de estágios",
        "Máquina de estados", "Contrato do checkpoint", "Semântica da retomada", "Semântica da invalidação", "Execução e falhas",
        "Persistência atômica e identidade", "Proteção de fontes", "Fronteira do adaptador de Git", "Configuração", "Compatibilidade de runtime",
        "Limitações conhecidas", "Fora do escopo", "Rastreabilidade",
    ]

    def test_required_sections(self) -> None:
        text = source(self.DOC)
        for section in self.SECTIONS:
            self.assertRegex(text, rf"(?m)^## {re.escape(section)}$", section)

    def test_names_every_new_module_schema_and_the_artifact(self) -> None:
        text = source(self.DOC)
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", text)
        for name in NEW_SCHEMAS:
            self.assertIn(f"schemas/{name}.schema.json", text)
        self.assertIn("docs/extraction/EXTRACTION_002.json", text)
        self.assertIn("schemas/agent-workflow.schema.json", text)

    def test_documents_every_state_and_every_transition(self) -> None:
        text = source(self.DOC)
        for state in ("PENDING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "INVALIDATED"):
            self.assertIn(state, text)
        for run_status in ("UNSAFE",):
            self.assertIn(run_status, text)
        from core.stage_state import TRANSITIONS

        for current, targets in TRANSITIONS.items():
            for target in targets:
                self.assertRegex(text, rf"{current}\s*(->|→)\s*[^\n]*{target}", f"{current} -> {target}")

    def test_documents_the_protection_classes(self) -> None:
        text = source(self.DOC)
        for word in ("READ_ONLY", "FROZEN", "MUTABLE_OUTPUT", "EACH_STAGE", "RUN_END"):
            self.assertIn(word, text)

    def test_states_the_runtime_facts_honestly(self) -> None:
        text = source(self.DOC)
        self.assertIn("3.15.0rc2", text)
        self.assertIn("3.10", text)
        self.assertRegex(text, r"(?i)linux|macos")
        self.assertRegex(text, r"(?i)não (foi|foram) (verificad|testad|executad)")

    def test_states_the_limits_that_matter(self) -> None:
        text = source(self.DOC).lower()
        for fragment in ("único escritor", "não é sandbox", "não segue", "detecta", "tempo limite"):
            self.assertIn(fragment, text, fragment)

    def test_architecture_lists_the_new_core_modules(self) -> None:
        text = source(REPO / "docs" / "ARCHITECTURE.md")
        for name in NEW_MODULES:
            self.assertIn(f"`{name}`", text)

    def test_schema_readme_lists_the_new_contracts(self) -> None:
        text = source(REPO / "schemas" / "README.md")
        for name in NEW_SCHEMAS:
            self.assertIn(f"`{name}`", text)


if __name__ == "__main__":
    unittest.main()
