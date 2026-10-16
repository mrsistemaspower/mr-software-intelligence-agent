"""Extração 3: acoplamento zero, sem provedor nem rede, determinismo, camadas, compatibilidade e rastreabilidade do AI_RECHECK."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, TempDirCase, load_json
from core import __version__, isolation
from core.serialization import canonical_pretty_bytes

NEW_MODULES = ["model_task", "model_adapter", "ai_recheck", "recheck_engine", "recheck_stage"]
NEW_SCHEMAS = ["model-task", "model-result", "ai-recheck-request", "ai-recheck-result"]
ADAPTER_MODULES = ["adapters/models/__init__.py", "adapters/models/base.py", "adapters/models/scripted.py"]
NEW_FILES = [REPO / "core" / f"{name}.py" for name in NEW_MODULES]
ADAPTER_FILES = [REPO / path for path in ADAPTER_MODULES]
SCHEMA_FILES = [REPO / "schemas" / f"{name}.schema.json" for name in NEW_SCHEMAS]
GENERIC_FILES = NEW_FILES + ADAPTER_FILES + SCHEMA_FILES
INTENDED_MINIMUM = (3, 10)
SYNTAX_AUDITED_DOWN_TO = (3, 9)
# Módulos de extrações POSTERIORES (a Extração 4 roteia revisão sobre o AI_RECHECK). Dependem dos desta extração por construção;
# a regra "nada anterior importa o novo" vale para os módulos que existiam antes, não para os que vieram depois.
LATER_MODULES = {"analysis_signal", "review_policy", "review_routing", "review_recheck"}

# Quem pode importar quem dentro do núcleo novo. Garante camadas sem ciclo e que as primitivas anteriores são reusadas.
ALLOWED_INTERNAL = {
    "model_task": {"hashing", "identity", "paths", "schema_lite", "serialization"},
    "model_adapter": {"hashing", "model_task", "paths", "serialization"},
    "ai_recheck": {"epistemics", "escalation", "hashing", "identity", "model_task", "paths", "serialization"},
    "recheck_engine": {"ai_recheck", "hashing", "model_adapter", "model_task", "stage_state"},
    "recheck_stage": {"ai_recheck", "model_adapter", "model_task", "orchestrator", "recheck_engine", "serialization", "stage_graph", "stage_state"},
}
PROVIDER_SDKS = {"anthropic", "openai", "google", "cohere", "mistralai", "boto3", "botocore", "litellm", "langchain", "transformers", "torch", "ollama", "httpx", "aiohttp", "requests", "urllib3"}
NETWORK = PROVIDER_SDKS | {"urllib", "http", "socket", "ssl", "asyncio", "ftplib", "smtplib", "xmlrpc", "websockets", "subprocess", "multiprocessing", "threading"}


class Quiet:
    """Mixin: uma falha de pertinência diz o que faltou, sem despejar arquivos inteiros na saída."""

    def assertIn(self, member, container, msg=None):  # noqa: N802
        if member not in container:
            raise self.failureException(msg or f"ausente: {member!r}")

    def assertNotIn(self, member, container, msg=None):  # noqa: N802
        if member in container:
            raise self.failureException(msg or f"presente: {member!r}")


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def tree(path: Path) -> ast.AST:
    return ast.parse(source(path), filename=path.name)


def imported(path: Path) -> set:
    names: set = set()
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class NoSyscomCouplingInTheGenericCore(Quiet, unittest.TestCase):
    """O executor de AI_RECHECK não conhece o projeto de referência, nenhum software, tela, data ou caminho."""

    FORBIDDEN = re.compile(
        r"\b(syscom|mr-syscom|mr_syscom|k3|k4|k3_4|k41|k42|cadastro|nota\s+fiscal|firebird|dokuwiki|dolphins|pilot|mr-ai)\b|\bscr-|k4[._]\d",
        re.IGNORECASE,
    )
    FIXED_DATE = re.compile(r"\b(19|20)\d\d-\d\d-\d\d\b")
    DRIVE_PATH = re.compile(r"[A-Za-z]:\\[A-Za-z]")
    HARDCODED_COUNT = re.compile(r"\b(total|expected|screens?)\s*[:=]\s*\d{2,}\b", re.IGNORECASE)

    def test_the_new_files_exist(self) -> None:
        for path in GENERIC_FILES + [REPO / "adapters" / "models" / "README.md"]:
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
        for sample in ("syscom", "SYSCOM", "mr-syscom", "k3", "K4", "k4.1", "Cadastro", "Nota Fiscal", "scr-01", "Firebird", "DokuWiki", "pilot"):
            self.assertIsNotNone(self.FORBIDDEN.search(f"x {sample} y"), sample)
        for innocent in ("hashing", "backup4", "recheck", "adapter", "evidence", "scripted", "stage"):
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

    def test_the_whole_repository_isolation_check_still_passes(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_all(REPO)], [])

    def test_no_branch_on_software_identity_in_the_core(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_core_independence(REPO)], [])

    def test_no_stage_name_or_workflow_vocabulary_is_special_to_the_core(self) -> None:
        reserved = {"inventory", "discover", "analyze", "validate", "ui-analysis", "database-analysis", "document"}
        for path in NEW_FILES + ADAPTER_FILES:
            constants = {n.value for n in ast.walk(tree(path)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            with self.subTest(file=path.name):
                self.assertEqual(constants & reserved, set())

    def test_software_ids_of_the_tests_are_not_in_the_generic_files(self) -> None:
        for path in GENERIC_FILES:
            text = source(path)
            with self.subTest(file=path.name):
                for term in ("synthetic-one", "synthetic-two", "acme"):
                    self.assertNotIn(term, text)

    def test_windows_specific_logic_stays_in_paths_py(self) -> None:
        pattern = re.compile(r"""["']nt["']|win32|msvcrt|winreg|ntpath|\\\\\\\\\?""")
        for path in NEW_FILES + ADAPTER_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(pattern.findall(source(path)), [])


class NoProviderNoNetworkNoCredentials(Quiet, TempDirCase):
    """Nenhum SDK, nenhuma chamada de API, nenhuma credencial: o contrato portátil vem antes de qualquer provedor."""

    def test_no_provider_name_in_the_core_the_schemas_or_the_neutral_adapters(self) -> None:
        terms = isolation.load_provider_terms(REPO)
        self.assertEqual([str(f) for f in isolation.scan_files(GENERIC_FILES + [REPO / "adapters" / "models" / "README.md"], terms, "PROVIDER", REPO)], [])
        self.assertEqual([str(f) for f in isolation.scan_provider_terms(REPO)], [])

    def test_the_provider_scan_has_teeth(self) -> None:
        terms = isolation.load_provider_terms(REPO)
        self.assertTrue({"claude", "openai", "anthropic", "kiro"} <= set(terms))

    def test_no_sdk_or_network_import_in_any_new_python_file(self) -> None:
        for path in NEW_FILES + ADAPTER_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(imported(path) & NETWORK, set())

    def test_importing_everything_loads_no_sdk_and_no_network_module(self) -> None:
        code = (
            "import sys\n"
            + "".join(f"import core.{name}\n" for name in NEW_MODULES)
            + "import adapters.models, adapters.models.base, adapters.models.scripted\n"
            + f"banned = {sorted(PROVIDER_SDKS | {'socket', 'ssl', 'http', 'urllib.request', 'asyncio', 'subprocess'})!r}\n"
            + "loaded = sorted(name for name in banned if name in sys.modules)\n"
            + "print(loaded)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tmp, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout.decode().strip(), "[]")

    def test_no_dependency_manifest_was_added(self) -> None:
        for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "package.json"):
            self.assertFalse((REPO / name).exists(), name)

    def test_no_credential_shape_in_any_new_file(self) -> None:
        pattern = re.compile(r"api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|bearer\s|sk-[A-Za-z0-9]{10,}|BEGIN (RSA|PRIVATE)", re.IGNORECASE)
        for path in GENERIC_FILES + [REPO / "adapters" / "models" / "README.md"]:
            with self.subTest(file=path.name):
                self.assertIsNone(pattern.search(source(path)))

    def test_no_environment_or_configuration_file_is_read(self) -> None:
        for path in NEW_FILES + ADAPTER_FILES:
            attributes = {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            with self.subTest(file=path.name):
                self.assertEqual(attributes & {"environ", "getenv", "expanduser", "home"}, set())

    def test_a_full_workflow_runs_with_every_network_primitive_forbidden(self) -> None:
        from tests.recheck_helpers import resolved
        from tests.test_recheck_stage import ALL_DONE, Flow
        from adapters.models.scripted import ScriptedModelAdapter

        boom = AssertionError("o executor tentou usar a rede")
        with mock.patch("socket.socket", side_effect=boom), mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch("socket.getaddrinfo", side_effect=boom), mock.patch("urllib.request.urlopen", side_effect=boom):
            flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]))
            self.assertEqual(flow.states(flow.run()), ALL_DONE)

    def test_the_real_provider_profiles_are_untouched_data(self) -> None:
        profiles = sorted((REPO / "adapters" / "llm").glob("*.profile.json"))
        self.assertGreaterEqual(len(profiles), 4)
        for path in profiles:
            self.assertEqual(path.suffix, ".json")
        self.assertFalse(list((REPO / "adapters" / "llm").glob("*.py")), "nenhum adaptador de provedor foi implementado")
        self.assertFalse(list((REPO / "adapters" / "llm").rglob("*.py")))


class DeterministicInputsOnly(Quiet, unittest.TestCase):
    """Nada de relógio, ambiente, acaso ou rede: o que entra no resultado é o que foi passado como argumento."""

    ALLOWED_IMPORTS = {"__future__", "base64", "binascii", "dataclasses", "functools", "re", "typing"}
    BANNED_ATTRIBUTES = {
        "environ", "getenv", "getlogin", "gethostname", "node", "now", "utcnow", "today", "uuid1", "uuid4", "time", "time_ns",
        "monotonic", "perf_counter", "getpid", "getuid", "getcwd", "random", "randint", "urandom", "system", "popen", "Popen", "sleep",
    }

    def test_only_the_standard_library_subset_is_imported(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertLessEqual(imported(path) - {"core"}, self.ALLOWED_IMPORTS)

    def test_no_primitive_of_earlier_extractions_is_reimplemented(self) -> None:
        for path in NEW_FILES + ADAPTER_FILES:
            with self.subTest(file=path.name):
                self.assertFalse(imported(path) & {"hashlib", "json", "secrets", "tempfile", "shutil", "uuid", "random", "time", "datetime"})

    def test_no_ambient_input_is_ever_read(self) -> None:
        for path in NEW_FILES + ADAPTER_FILES:
            attributes = {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            with self.subTest(file=path.name):
                self.assertEqual(attributes & self.BANNED_ATTRIBUTES, set())

    def test_the_ambient_scan_has_teeth(self) -> None:
        sample = ast.parse("import datetime, os\nx = datetime.datetime.now()\ny = os.environ['A']\nz = os.getcwd()\n")
        attributes = {n.attr for n in ast.walk(sample) if isinstance(n, ast.Attribute)}
        self.assertEqual(attributes & self.BANNED_ATTRIBUTES, {"now", "environ", "getcwd"})

    def test_the_executor_has_no_clock_and_no_sleep_so_no_internal_retry_loop(self) -> None:
        for name in ("recheck_engine", "recheck_stage", "ai_recheck"):
            text = source(REPO / "core" / f"{name}.py")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", '"', "-", "|")))
            with self.subTest(name):
                self.assertNotIn("sleep", code)
                self.assertNotRegex(code, r"\bwhile\b")

    def test_the_adapter_is_called_at_most_once_per_attempt_by_construction(self) -> None:
        engine = tree(REPO / "core" / "recheck_engine.py")
        calls = [n for n in ast.walk(engine) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"]
        self.assertEqual(len(calls), 1)


class LayeringAndReuse(Quiet, unittest.TestCase):
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

    def test_the_primitives_of_earlier_extractions_are_reused_where_they_belong(self) -> None:
        expectations = {
            "model_task": {"hashing", "identity", "serialization", "schema_lite"},
            "ai_recheck": {"epistemics", "escalation", "identity"},
            "recheck_engine": {"hashing", "stage_state"},
            "recheck_stage": {"orchestrator", "stage_state"},
        }
        for name, needed in expectations.items():
            with self.subTest(name):
                self.assertLessEqual(needed, self.internal_imports(REPO / "core" / f"{name}.py"))

    def test_the_core_never_imports_the_adapters_layer(self) -> None:
        for path in sorted((REPO / "core").glob("*.py")):
            with self.subTest(file=path.name):
                self.assertNotIn("adapters", imported(path))

    def test_nothing_earlier_imports_the_new_modules_and_the_orchestration_base_knows_nothing_about_ai_recheck(self) -> None:
        new = set(NEW_MODULES)
        # A Foundation (epistemics, escalation, pack, impact...) já nomeia AI_RECHECK como uma das quatro rotas, desde o início.
        # O que não pode existir é a BASE de orquestração e as primitivas das Extrações 1 e 2 saberem de recheck ou de modelo.
        base = {
            "orchestrator", "resume", "run_state", "run_config", "checkpoint", "protection", "stage_graph", "stage_state", "gitstate",
            "atomic", "snapshot", "integrity", "identity", "hashing", "serialization", "paths",
        }
        for path in sorted((REPO / "core").glob("*.py")):
            if path.stem in new or path.stem in LATER_MODULES:
                continue
            with self.subTest(file=path.name):
                self.assertEqual(self.internal_imports(path) & new, set())
                if path.stem in base:
                    self.assertNotIn("recheck", source(path).lower())
                    self.assertNotIn("model_adapter", source(path).lower())

    def test_no_second_orchestration_framework(self) -> None:
        text = source(REPO / "core" / "recheck_stage.py")
        self.assertNotIn("class Orchestrator", text)
        self.assertNotIn("StageGraph(", text)
        self.assertIn("from .orchestrator import", text)
        stage_module = tree(REPO / "core" / "recheck_stage.py")
        classes = {n.name for n in ast.walk(stage_module) if isinstance(n, ast.ClassDef)}
        self.assertEqual(classes, {"_StageEvidenceReader", "AIRecheckStage"})

    def test_schema_registry_knows_the_new_contracts(self) -> None:
        from core.schema_lite import Registry

        registry = Registry()
        for name in NEW_SCHEMAS:
            self.assertIn(f"urn:mr-software-intelligence:schema:{name}:1", registry.by_id)
        self.assertEqual(registry.check_all(), [])

    def test_the_new_schemas_compose_instead_of_duplicating(self) -> None:
        for name in ("model-result", "ai-recheck-request", "ai-recheck-result"):
            text = source(REPO / "schemas" / f"{name}.schema.json")
            with self.subTest(name):
                self.assertIn("urn:mr-software-intelligence:schema:model-task:1#/$defs/", text)
        for name in ("ai-recheck-request", "ai-recheck-result"):
            self.assertIn("urn:mr-software-intelligence:schema:common:1#/$defs/", source(REPO / "schemas" / f"{name}.schema.json"))
        result = source(REPO / "schemas" / "ai-recheck-result.schema.json")
        self.assertIn("urn:mr-software-intelligence:schema:model-result:1#/$defs/adapterIdentity", result)

    def test_each_new_schema_passes_the_meta_check_individually(self) -> None:
        from core.schema_lite import Registry

        registry = Registry()
        for name in NEW_SCHEMAS:
            with self.subTest(name):
                self.assertEqual(registry.check_schema(registry.get(name)), [])


class RuntimeCompatibility(Quiet, unittest.TestCase):
    """Mínimo pretendido 3.10; sintaxe auditada até 3.9. Só o interpretador de hoje executa."""

    PYTHON_FILES = NEW_FILES + ADAPTER_FILES

    def test_syntax_is_valid_for_the_audited_floor(self) -> None:
        for path in self.PYTHON_FILES:
            with self.subTest(file=path.name):
                ast.parse(source(path), filename=path.name, feature_version=SYNTAX_AUDITED_DOWN_TO)

    def test_no_api_or_keyword_newer_than_the_audited_floor(self) -> None:
        newer = {"bit_count", "pairwise", "batched", "file_digest", "StrEnum", "ExceptionGroup", "TaskGroup", "tomllib", "Self", "override",
                 "TypeAlias", "ParamSpec", "Required", "NotRequired", "LiteralString", "Never", "assert_never"}
        newer_keywords = {"strict", "slots", "kw_only", "match_args"}
        for path in self.PYTHON_FILES:
            module = tree(path)
            names = {n.attr for n in ast.walk(module) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(module) if isinstance(n, ast.Name)}
            keywords = {k.arg for n in ast.walk(module) if isinstance(n, ast.Call) for k in n.keywords}
            with self.subTest(file=path.name):
                self.assertEqual(names & newer, set())
                self.assertEqual(keywords & newer_keywords, set())

    def test_no_release_candidate_only_feature_is_used(self) -> None:
        for path in self.PYTHON_FILES:
            self.assertNotIn("sys.version_info", source(path))

    def test_annotations_are_lazy_so_modern_union_syntax_never_runs_on_older_pythons(self) -> None:
        for path in self.PYTHON_FILES:
            if path.name == "__init__.py":
                continue
            with self.subTest(file=path.name):
                self.assertIn("from __future__ import annotations", source(path))

    def test_no_runtime_use_of_union_operator_on_types(self) -> None:
        for path in self.PYTHON_FILES:
            for node in ast.walk(tree(path)):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "isinstance" and len(node.args) == 2:
                    with self.subTest(file=path.name, line=node.lineno):
                        self.assertNotIsInstance(node.args[1], ast.BinOp)

    def test_declared_versions(self) -> None:
        self.assertLessEqual(SYNTAX_AUDITED_DOWN_TO, INTENDED_MINIMUM)
        self.assertGreaterEqual(sys.version_info[:2], INTENDED_MINIMUM)

    def test_core_version_was_bumped_for_the_new_capability(self) -> None:
        self.assertGreaterEqual(tuple(int(part) for part in __version__.split(".")), (1, 3, 0))


class ImportHasNoSideEffects(Quiet, TempDirCase):
    def test_importing_every_module_prints_and_writes_nothing(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        code = "import " + ", ".join([f"core.{name}" for name in NEW_MODULES] + ["adapters.models.base", "adapters.models.scripted"])
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tmp, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_every_new_module_documents_itself(self) -> None:
        for path in NEW_FILES + ADAPTER_FILES:
            doc = ast.get_docstring(tree(path))
            with self.subTest(file=path.name):
                self.assertTrue(doc and len(doc) > (60 if path.name == "__init__.py" else 120))


TRACE = REPO / "docs" / "extraction" / "EXTRACTION_003.json"
CLASSES = {"GENERIC_BEHAVIOR", "SYSCOM_COUPLING", "DATASET_SPECIFIC", "HISTORICAL_ACCIDENT"}
CAPABILITY_FIELDS = {"id", "capability", "source_reference", "source_classification", "generic_invariant", "agent_destination", "rejected_coupling", "tests"}
STUDIED = {
    "tools/review/derive_review_class.py", "tools/batch/batch_run.py", "tools/batch/k4_2_review.py", "tools/batch/k4_2_correction.py",
}


class TraceabilityArtifact(Quiet, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.trace = load_json(TRACE)

    def test_header(self) -> None:
        trace = self.trace
        self.assertEqual(trace["extraction"], "EXTRACTION_003")
        self.assertEqual(trace["base_commit"], "49a05b266908a5ef3d45650286d7cecd133f2559")
        reference = trace["reference"]
        self.assertEqual(reference["access"], "read-only")
        self.assertEqual(reference["commit"], "4c652a38c37d1c492837e82a0cef06de59de2a6c")
        self.assertEqual({item["path"] for item in reference["sources"]}, STUDIED)
        for item in reference["sources"]:
            self.assertRegex(item["git_blob"], r"^[0-9a-f]{40}$")

    def test_every_capability_has_every_required_field(self) -> None:
        self.assertGreaterEqual(len(self.trace["capabilities"]), 10)
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
            self.assertRegex(identifier, r"^EX3-\d\d$")

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

    def test_every_new_module_adapter_and_schema_is_traced_to_a_capability(self) -> None:
        destinations = {d["file"] for c in self.trace["capabilities"] for d in c["agent_destination"]}
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", destinations)
        for name in NEW_SCHEMAS:
            self.assertIn(f"schemas/{name}.schema.json", destinations)
        for path in ("adapters/models/base.py", "adapters/models/scripted.py"):
            self.assertIn(path, destinations)

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
        for word in ("revisão humana", "ingestão de resposta humana", "dokuwiki", "daemon", "k4.3", "analisador de telas", "engenharia reversa de banco",
                     "interface web", "adaptador real de provedor", "api de modelo", "credencial"):
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

    def test_the_extraction_two_deferred_ai_recheck_item_is_the_one_delivered_here(self) -> None:
        previous = load_json(REPO / "docs" / "extraction" / "EXTRACTION_002.json")
        deferred = [item for item in previous["deferred"] if "AI_RECHECK" in item["planned_destination"]]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["source_reference"]["symbols"], ["op_review", "op_recheck"])
        covered = {symbol for c in self.trace["capabilities"] for symbol in c["source_reference"]["symbols"]}
        self.assertTrue({"op_review", "op_recheck"} <= covered)


class DocumentationOfTheExtraction(Quiet, unittest.TestCase):
    DOC = REPO / "docs" / "AGENT_EXTRACTION_3.md"
    SECTIONS = [
        "Definição de AI_RECHECK", "Fontes estudadas", "Invariantes genéricos extraídos", "Acoplamento do SYSCOM rejeitado",
        "Fronteira do adaptador de modelo", "Tarefa de modelo", "Pedido", "Resultado e estados", "Regras de evidência",
        "Validação da resposta", "Teto epistêmico", "Roteamento", "Elegibilidade do adaptador: capacidades e fronteira de dados",
        "Semântica de repetição", "Estágio de orquestração", "Trilha de auditoria e determinismo", "Perfis de provedor e fronteiras futuras",
        "Compatibilidade de runtime", "Limitações conhecidas", "Fora do escopo", "Rastreabilidade",
    ]

    def test_required_sections(self) -> None:
        text = source(self.DOC)
        for section in self.SECTIONS:
            self.assertRegex(text, rf"(?m)^## {re.escape(section)}$", section)

    def test_names_every_new_module_adapter_schema_and_the_artifact(self) -> None:
        text = source(self.DOC)
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", text)
        for name in NEW_SCHEMAS:
            self.assertIn(f"schemas/{name}.schema.json", text)
        for path in ("adapters/models/base.py", "adapters/models/scripted.py"):
            self.assertIn(path, text)
        self.assertIn("docs/extraction/EXTRACTION_003.json", text)

    def test_documents_every_status_route_destination_and_failure_kind(self) -> None:
        from core import ai_recheck

        text = source(self.DOC)
        for word in ai_recheck.STATUSES + ai_recheck.ROUTES + ai_recheck.FAILURE_KINDS + tuple(sorted(set(ai_recheck.DESTINATIONS.values()))):
            self.assertIn(word, text, word)
        for word in ("LOCAL", "PRIVATE", "EXTERNAL", "RECHECK_LOOP", "VALIDATED_PRIOR", "INFERRED", "DETERMINISTIC_ADAPTER", "NOT_GUARANTEED"):
            self.assertIn(word, text, word)

    def test_states_the_epistemic_rule_and_the_boundary_with_humans(self) -> None:
        text = source(self.DOC)
        self.assertRegex(text, r"(?i)nunca cria validação humana")
        self.assertRegex(text, r"(?i)não pode[^\n]*\n?[^\n]*perguntar a uma pessoa|perguntar a uma pessoa")
        self.assertRegex(text, r"(?i)candidato")
        self.assertRegex(text, r"(?i)referência fora da lista[^\n]*invalida a resposta INTEIRA")

    def test_states_the_runtime_facts_honestly(self) -> None:
        text = source(self.DOC)
        self.assertIn("3.15.0rc2", text)
        self.assertIn("3.10", text)
        self.assertRegex(text, r"(?i)linux|macos")
        self.assertRegex(text, r"(?i)não (foi|foram) (verificad|testad|executad)")

    def test_states_the_limits_that_matter(self) -> None:
        text = source(self.DOC).lower()
        for fragment in ("não existe adaptador real", "retryable", "sem isolamento nem tempo limite", "não detecta dado sensível", "dlp"):
            self.assertIn(fragment, text, fragment)

    def test_architecture_lists_the_new_core_modules_and_adapters(self) -> None:
        text = source(REPO / "docs" / "ARCHITECTURE.md")
        for name in NEW_MODULES:
            self.assertIn(f"`{name}`", text)
        self.assertIn("adapters/models/", text)
        self.assertIn("AGENT_EXTRACTION_3.md", text)

    def test_schema_readme_lists_the_new_contracts(self) -> None:
        text = source(REPO / "schemas" / "README.md")
        for name in NEW_SCHEMAS:
            self.assertIn(f"`{name}`", text)

    def test_the_adapters_readme_documents_the_boundary_and_stays_provider_neutral(self) -> None:
        text = source(REPO / "adapters" / "models" / "README.md")
        for word in ("execute(task)", "descriptor", "data_boundary", "capabilities", "deterministic", "adapters/llm/"):
            self.assertIn(word, text, word)
        self.assertIn("models/", source(REPO / "adapters" / "README.md"))

    def test_the_main_readme_records_the_third_extraction(self) -> None:
        text = source(REPO / "README.md")
        self.assertIn("Agent Extraction 3", text)
        self.assertIn("EXTRACTION_003.json", text)


if __name__ == "__main__":
    unittest.main()
