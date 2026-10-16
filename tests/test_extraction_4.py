"""Extração 4: acoplamento zero, sem provedor nem rede, determinismo, camadas, compatibilidade e rastreabilidade do roteamento de revisão."""
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
from core.analysis_signal import BASE_FAMILIES
from core.serialization import canonical_pretty_bytes

NEW_MODULES = ["analysis_signal", "review_policy", "review_routing", "review_recheck"]
NEW_SCHEMAS = ["analysis-signal", "review-decision", "source-gap-requirement", "human-decision-candidate"]
POLICY_FILE = REPO / "policies" / "review-routing.policy.json"
RETRY_FIX_MODULES = ["stage_state", "orchestrator", "resume", "recheck_stage"]
NEW_FILES = [REPO / "core" / f"{name}.py" for name in NEW_MODULES]
SCHEMA_FILES = [REPO / "schemas" / f"{name}.schema.json" for name in NEW_SCHEMAS]
GENERIC_FILES = NEW_FILES + SCHEMA_FILES + [POLICY_FILE]
INTENDED_MINIMUM = (3, 10)
SYNTAX_AUDITED_DOWN_TO = (3, 9)

# Quem pode importar quem dentro do núcleo novo. O roteamento puro não conhece o contrato do modelo; só a ponte conhece.
ALLOWED_INTERNAL = {
    "analysis_signal": {"epistemics", "identity", "model_task", "paths"},
    "review_policy": {"analysis_signal", "hashing", "model_task", "serialization"},
    "review_routing": {"analysis_signal", "hashing", "identity", "model_task", "paths", "review_policy", "serialization"},
    "review_recheck": {"ai_recheck", "analysis_signal", "model_task", "review_policy", "review_routing"},
}
PROVIDER_SDKS = {"anthropic", "openai", "google", "cohere", "mistralai", "boto3", "botocore", "litellm", "langchain", "transformers", "torch", "ollama", "httpx", "aiohttp", "requests", "urllib3"}
NETWORK = PROVIDER_SDKS | {"urllib", "http", "socket", "ssl", "asyncio", "ftplib", "smtplib", "xmlrpc", "websockets", "subprocess", "multiprocessing", "threading"}
WIDGET_WORDS = {"screen", "button", "tab", "tabs", "grid", "combobox", "lookup", "menu", "menu_path", "column", "columns", "dialog"}


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


def internal_imports(path: Path) -> set:
    found: set = set()
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module)
            else:
                found.update(alias.name for alias in node.names)
    return found


def string_constants(path: Path) -> set:
    return {node.value for node in ast.walk(tree(path)) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def schema_words(node: object) -> set:
    """Nomes de propriedade e valores de enum de um schema (o vocabulário que ele expõe a quem o usa)."""
    found: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                found.update(value)
            if key == "enum" and isinstance(value, list):
                found.update(item for item in value if isinstance(item, str))
            found |= schema_words(value)
    elif isinstance(node, list):
        for value in node:
            found |= schema_words(value)
    return found


class NoReferenceCouplingInTheGenericFiles(Quiet, unittest.TestCase):
    """O roteamento não conhece o projeto de referência, nenhum software, tela, data ou caminho."""

    FORBIDDEN = re.compile(
        r"\b(syscom|mr-syscom|mr_syscom|k3|k4|k3_4|k41|k42|cadastro|nota\s+fiscal|firebird|dokuwiki|dolphins|pilot|mr-ai|d8|d9)\b|\bscr-|k4[._]\d",
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
        self.assertEqual(hits, [], "acoplamento ao projeto de referência no roteamento genérico")

    def test_the_term_scan_has_teeth(self) -> None:
        for sample in ("syscom", "SYSCOM", "mr-syscom", "k3", "K4", "k4.1", "Cadastro", "Nota Fiscal", "scr-01", "Firebird", "DokuWiki", "pilot", "D8", "D9"):
            self.assertIsNotNone(self.FORBIDDEN.search(f"x {sample} y"), sample)
        for innocent in ("hashing", "backup4", "recheck", "adapter", "evidence", "routing", "policy", "decision", "d80", "id9x"):
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

    def test_no_screen_or_widget_vocabulary_in_any_identifier_key_or_enum(self) -> None:
        for path in NEW_FILES:
            module = tree(path)
            names = {n.id for n in ast.walk(module) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(module) if isinstance(n, ast.Attribute)}
            names |= {n.name for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            names |= {n.arg for n in ast.walk(module) if isinstance(n, ast.arg)}
            names |= {k.arg for n in ast.walk(module) if isinstance(n, ast.Call) for k in n.keywords if k.arg}
            names |= {s for s in string_constants(path) if re.fullmatch(r"[a-z_]+", s)}
            with self.subTest(file=path.name):
                self.assertEqual({name.lower() for name in names} & WIDGET_WORDS, set())
        for path in SCHEMA_FILES + [POLICY_FILE]:
            with self.subTest(file=path.name):
                self.assertEqual({word.lower() for word in schema_words(load_json(path))} & WIDGET_WORDS, set())

    def test_software_ids_of_the_tests_are_not_in_the_generic_files(self) -> None:
        for path in GENERIC_FILES:
            text = source(path)
            with self.subTest(file=path.name):
                for term in ("synthetic-one", "synthetic-two", "acme"):
                    self.assertNotIn(term, text)

    def test_no_stage_or_workflow_vocabulary_is_special_to_the_core(self) -> None:
        reserved = {"inventory", "discover", "analyze", "validate", "ui-analysis", "database-analysis", "document"}
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(string_constants(path) & reserved, set())

    def test_windows_specific_logic_stays_in_paths_py(self) -> None:
        pattern = re.compile(r"""["']nt["']|win32|msvcrt|winreg|ntpath|\\\\\\\\\?""")
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(pattern.findall(source(path)), [])


class RoutingRulesLiveInThePolicyNotInTheCode(Quiet, unittest.TestCase):
    def test_no_family_name_is_a_constant_of_the_routing_code(self) -> None:
        for name in ("review_policy", "review_routing", "review_recheck"):
            with self.subTest(name):
                self.assertEqual(string_constants(REPO / "core" / f"{name}.py") & set(BASE_FAMILIES), set(), "o roteamento ramifica por família")

    def test_no_effect_name_or_impact_class_is_a_constant_of_the_routing_code(self) -> None:
        policy = load_json(POLICY_FILE)
        names = set(policy["impact"]["effects"]) | set(policy["impact"]["order"]) - {"informational"}
        for name in ("review_policy", "review_routing", "review_recheck"):
            with self.subTest(name):
                self.assertEqual(string_constants(REPO / "core" / f"{name}.py") & names, set())

    def test_the_default_policy_knows_exactly_the_base_families(self) -> None:
        keys = {key.split(":")[0] for key in load_json(POLICY_FILE)["rules"]}
        self.assertEqual(keys, set(BASE_FAMILIES))

    def test_the_order_between_classes_is_fixed_in_code_and_documented(self) -> None:
        text = source(REPO / "core" / "review_routing.py")
        self.assertIn("_CLASS_OF_TIER", text)
        self.assertIn("a política não a reordena", text)
        self.assertNotIn("precedence", load_json(POLICY_FILE)["rules"])


class NoProviderNoNetworkNoCredentials(Quiet, TempDirCase):
    def test_no_provider_name_in_the_core_the_schemas_or_the_policy(self) -> None:
        terms = isolation.load_provider_terms(REPO)
        self.assertEqual([str(f) for f in isolation.scan_files(GENERIC_FILES, terms, "PROVIDER", REPO)], [])
        self.assertEqual([str(f) for f in isolation.scan_provider_terms(REPO)], [])

    def test_no_sdk_or_network_import_in_any_new_python_file(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(imported(path) & NETWORK, set())

    def test_importing_everything_loads_no_sdk_and_no_network_module(self) -> None:
        code = (
            "import sys\n"
            + "".join(f"import core.{name}\n" for name in NEW_MODULES)
            + f"banned = {sorted(PROVIDER_SDKS | {'socket', 'ssl', 'http', 'urllib.request', 'asyncio', 'subprocess'})!r}\n"
            + "print(sorted(name for name in banned if name in sys.modules))\n"
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
        for path in GENERIC_FILES:
            with self.subTest(file=path.name):
                self.assertIsNone(pattern.search(source(path)))

    def test_no_environment_or_configuration_file_is_read(self) -> None:
        for path in NEW_FILES:
            attributes = {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            with self.subTest(file=path.name):
                self.assertEqual(attributes & {"environ", "getenv", "expanduser", "home"}, set())


class DeterministicAndPureByConstruction(Quiet, unittest.TestCase):
    ALLOWED_IMPORTS = {"__future__", "dataclasses", "functools", "pathlib", "re", "typing"}
    BANNED_ATTRIBUTES = {
        "environ", "getenv", "getlogin", "gethostname", "node", "now", "utcnow", "today", "uuid1", "uuid4", "time", "time_ns",
        "monotonic", "perf_counter", "getpid", "getuid", "getcwd", "random", "randint", "urandom", "system", "popen", "Popen", "sleep",
    }

    def test_only_the_standard_library_subset_is_imported(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertLessEqual(imported(path) - {"core"}, self.ALLOWED_IMPORTS)

    def test_no_primitive_of_earlier_extractions_is_reimplemented(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertFalse(imported(path) & {"hashlib", "json", "secrets", "tempfile", "shutil", "uuid", "random", "time", "datetime", "os", "subprocess"})

    def test_no_ambient_input_is_ever_read(self) -> None:
        for path in NEW_FILES:
            attributes = {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute)}
            with self.subTest(file=path.name):
                self.assertEqual(attributes & self.BANNED_ATTRIBUTES, set())

    def test_only_the_policy_loader_reads_a_file_and_the_router_reads_none(self) -> None:
        for path in NEW_FILES:
            reads = {n.attr for n in ast.walk(tree(path)) if isinstance(n, ast.Attribute) and n.attr in {"read_bytes", "read_text", "open", "write_bytes", "write_text", "iterdir", "glob", "rglob", "listdir", "walk"}}
            with self.subTest(file=path.name):
                self.assertEqual(reads, {"read_bytes"} if path.stem == "review_policy" else set())

    def test_no_loop_that_could_repeat_an_analysis_and_no_sleep(self) -> None:
        for path in NEW_FILES:
            module = tree(path)
            with self.subTest(file=path.name):
                self.assertEqual([n for n in ast.walk(module) if isinstance(n, ast.While)], [])
                self.assertNotIn("sleep", source(path))

    def test_the_routing_functions_call_no_model_and_no_executor(self) -> None:
        for name in ("review_policy", "review_routing"):
            self.assertNotIn("execute", {n.attr for n in ast.walk(tree(REPO / "core" / f"{name}.py")) if isinstance(n, ast.Attribute)})
        bridge = {n.attr for n in ast.walk(tree(REPO / "core" / "review_recheck.py")) if isinstance(n, ast.Attribute)}
        self.assertNotIn("execute", bridge)
        self.assertNotIn("run_recheck", string_constants(REPO / "core" / "review_recheck.py"))

    def test_the_ambient_scan_has_teeth(self) -> None:
        sample = ast.parse("import datetime, os\nx = datetime.datetime.now()\ny = os.environ['A']\nz = os.getcwd()\n")
        attributes = {n.attr for n in ast.walk(sample) if isinstance(n, ast.Attribute)}
        self.assertEqual(attributes & self.BANNED_ATTRIBUTES, {"now", "environ", "getcwd"})


class LayeringAndReuse(Quiet, unittest.TestCase):
    def test_every_new_module_is_in_the_layering_table(self) -> None:
        self.assertEqual(set(ALLOWED_INTERNAL), set(NEW_MODULES))

    def test_internal_imports_follow_the_layering(self) -> None:
        for name in NEW_MODULES:
            with self.subTest(name):
                self.assertLessEqual(internal_imports(REPO / "core" / f"{name}.py"), ALLOWED_INTERNAL[name])

    def test_the_layering_has_no_cycle(self) -> None:
        graph = {name: {dep for dep in deps if dep in ALLOWED_INTERNAL} for name, deps in ALLOWED_INTERNAL.items()}
        done: set = set()
        while len(done) < len(graph):
            ready = [name for name in graph if name not in done and graph[name] <= done]
            self.assertTrue(ready, "ciclo entre módulos")
            done.update(ready)

    def test_the_primitives_of_earlier_extractions_are_reused_where_they_belong(self) -> None:
        expectations = {
            "analysis_signal": {"epistemics", "identity", "model_task"},
            "review_policy": {"hashing", "serialization"},
            "review_routing": {"hashing", "identity", "serialization", "review_policy"},
            "review_recheck": {"ai_recheck", "review_routing"},
        }
        for name, needed in expectations.items():
            with self.subTest(name):
                self.assertLessEqual(needed, internal_imports(REPO / "core" / f"{name}.py"))

    def test_only_the_bridge_knows_the_ai_recheck_contract_and_nobody_knows_the_executor(self) -> None:
        for name in NEW_MODULES:
            imports = internal_imports(REPO / "core" / f"{name}.py")
            with self.subTest(name):
                self.assertEqual(imports & {"recheck_engine", "recheck_stage", "model_adapter", "orchestrator"}, set())
                self.assertEqual("ai_recheck" in imports, name == "review_recheck")

    def test_nothing_earlier_imports_the_new_modules(self) -> None:
        for path in sorted((REPO / "core").glob("*.py")):
            if path.stem in NEW_MODULES:
                continue
            with self.subTest(file=path.name):
                self.assertEqual(internal_imports(path) & set(NEW_MODULES), set())

    def test_the_orchestration_base_knows_nothing_about_review_routing(self) -> None:
        for name in RETRY_FIX_MODULES + ["run_state", "run_config", "checkpoint", "stage_graph"]:
            text = source(REPO / "core" / f"{name}.py").lower()
            with self.subTest(name):
                for word in ("review_routing", "analysis_signal", "review_policy", "review_recheck", "reviewdecision"):
                    self.assertNotIn(word, text)

    def test_no_second_orchestration_framework(self) -> None:
        for path in NEW_FILES:
            text = source(path)
            with self.subTest(file=path.name):
                for word in ("class Orchestrator", "StageGraph(", "StageSpec(", "RunConfig("):
                    self.assertNotIn(word, text)

    def test_the_foundation_escalation_ladder_is_untouched_and_not_duplicated(self) -> None:
        text = source(REPO / "core" / "escalation.py")
        self.assertIn("def decide(", text)
        self.assertIn("HUMAN_VALIDATION", text)
        for path in NEW_FILES:
            self.assertNotIn("def decide(", source(path))

    def test_schema_registry_knows_the_new_contracts(self) -> None:
        from core.schema_lite import Registry

        registry = Registry()
        for name in NEW_SCHEMAS:
            self.assertIn(f"urn:mr-software-intelligence:schema:{name}:1", registry.by_id)
        self.assertEqual(registry.check_all(), [])

    def test_the_new_schemas_compose_instead_of_duplicating(self) -> None:
        for name in NEW_SCHEMAS:
            with self.subTest(name):
                self.assertIn("urn:mr-software-intelligence:schema:model-task:1#/$defs/", source(REPO / "schemas" / f"{name}.schema.json"))
        for name in ("source-gap-requirement", "human-decision-candidate", "review-decision"):
            self.assertIn("urn:mr-software-intelligence:schema:analysis-signal:1#/$defs/", source(REPO / "schemas" / f"{name}.schema.json"))
        decision = source(REPO / "schemas" / "review-decision.schema.json")
        for name in ("analysis-signal", "source-gap-requirement", "human-decision-candidate"):
            self.assertIn(f"urn:mr-software-intelligence:schema:{name}:1", decision)
        self.assertIn("urn:mr-software-intelligence:schema:common:1#/$defs/", source(REPO / "schemas" / "human-decision-candidate.schema.json"))

    def test_there_is_no_schema_explosion(self) -> None:
        from core.schema_lite import Registry

        schema_names = {Path(p).name[: -len(".schema.json")] for p in Registry().path_by_id.values()}
        added = {name for name in schema_names if name.startswith(("review", "analysis", "source-gap", "human-decision"))}
        self.assertEqual(added, set(NEW_SCHEMAS))

    def test_each_new_schema_passes_the_meta_check_individually(self) -> None:
        from core.schema_lite import Registry

        registry = Registry()
        for name in NEW_SCHEMAS:
            with self.subTest(name):
                self.assertEqual(registry.check_schema(registry.get(name)), [])

    def test_the_closed_vocabularies_are_the_ones_of_earlier_layers(self) -> None:
        from core import review_routing
        from core.model_task import BOUNDARIES

        common = load_json(REPO / "schemas" / "common.schema.json")["$defs"]
        self.assertEqual(list(review_routing.LADDER_STEPS), common["ladderStep"]["enum"])
        self.assertEqual(load_json(REPO / "schemas" / "model-task.schema.json")["$defs"]["boundary"]["enum"], list(BOUNDARIES))
        self.assertEqual(
            list(review_routing.RECHECK_STATUSES), [item for item in load_json(REPO / "schemas" / "ai-recheck-result.schema.json")["properties"]["status"]["enum"] if item != "FAILED"]
        )


class RuntimeCompatibility(Quiet, unittest.TestCase):
    """Mínimo pretendido 3.10; sintaxe auditada até 3.9. Só o interpretador de hoje executa."""

    PYTHON_FILES = NEW_FILES + [REPO / "core" / f"{name}.py" for name in RETRY_FIX_MODULES]

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

    def test_no_match_statement_and_no_version_probe(self) -> None:
        for path in self.PYTHON_FILES:
            with self.subTest(file=path.name):
                self.assertEqual([n for n in ast.walk(tree(path)) if type(n).__name__ == "Match"], [])
                self.assertNotIn("sys.version_info", source(path))

    def test_annotations_are_lazy_so_modern_union_syntax_never_runs_on_older_pythons(self) -> None:
        for path in self.PYTHON_FILES:
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
        self.assertGreaterEqual(tuple(int(part) for part in __version__.split(".")), (1, 4, 0))


class ImportHasNoSideEffects(Quiet, TempDirCase):
    def test_importing_every_module_prints_writes_and_reads_nothing(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        code = (
            "import " + ", ".join(f"core.{name}" for name in NEW_MODULES) + "\n"
            "import core.review_policy as p\n"
            "assert p.default_policy.cache_info().currsize == 0, 'a política não pode ser carregada no import'\n"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tmp, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_every_new_module_documents_itself(self) -> None:
        for path in NEW_FILES:
            doc = ast.get_docstring(tree(path))
            with self.subTest(file=path.name):
                self.assertTrue(doc and len(doc) > 400)


TRACE = REPO / "docs" / "extraction" / "EXTRACTION_004.json"
CLASSES = {"GENERIC_BEHAVIOR", "SYSCOM_COUPLING", "DATASET_SPECIFIC", "HISTORICAL_ACCIDENT"}
CAPABILITY_FIELDS = {"id", "capability", "source_reference", "source_classification", "generic_invariant", "agent_destination", "rejected_coupling", "tests"}
STUDIED = {"docs/HUMAN_REVIEW_POLICY.md", "tools/review/derive_review_class.py"}


class TraceabilityArtifact(Quiet, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.trace = load_json(TRACE)

    def test_header(self) -> None:
        trace = self.trace
        self.assertEqual(trace["extraction"], "EXTRACTION_004")
        self.assertEqual(trace["base_commit"], "39924005b8ede7068e9940d6efb34af526ae84b6")
        reference = trace["reference"]
        self.assertEqual(reference["access"], "read-only")
        self.assertEqual(reference["commit"], "4c652a38c37d1c492837e82a0cef06de59de2a6c")
        self.assertEqual({item["path"] for item in reference["sources"]}, STUDIED)
        for item in reference["sources"]:
            self.assertRegex(item["git_blob"], r"^[0-9a-f]{40}$")
        self.assertTrue(reference["not_consulted"].strip())

    def test_every_capability_has_every_required_field(self) -> None:
        self.assertGreaterEqual(len(self.trace["capabilities"]), 12)
        for capability in self.trace["capabilities"]:
            with self.subTest(capability["id"]):
                self.assertTrue(CAPABILITY_FIELDS <= set(capability), CAPABILITY_FIELDS - set(capability))
                for field in ("capability", "generic_invariant"):
                    self.assertTrue(capability[field].strip())
                self.assertTrue(capability["rejected_coupling"], "toda extração rejeita algum acoplamento")
                self.assertTrue(capability["tests"])
                self.assertTrue(set(capability["source_classification"]) <= CLASSES and capability["source_classification"])
                self.assertTrue(capability["source_reference"]["file"] and capability["source_reference"]["symbols"])

    def test_ids_are_unique_and_ordered(self) -> None:
        ids = [c["id"] for c in self.trace["capabilities"]]
        self.assertEqual(ids, sorted(set(ids)))
        for identifier in ids:
            self.assertRegex(identifier, r"^EX4-\d\d$")

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

    def test_every_new_module_schema_and_the_policy_are_traced_to_a_capability(self) -> None:
        destinations = {d["file"] for c in self.trace["capabilities"] for d in c["agent_destination"]}
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", destinations)
        for name in NEW_SCHEMAS:
            self.assertIn(f"schemas/{name}.schema.json", destinations)
        self.assertIn("policies/review-routing.policy.json", destinations)

    def test_referenced_tests_exist(self) -> None:
        references = [test for c in self.trace["capabilities"] for test in c["tests"]]
        references += [test for item in self.trace["corrections"] for test in item["tests"]]
        for reference in references:
            file, _, cls = reference.partition("::")
            path = REPO / file
            with self.subTest(reference):
                self.assertTrue(path.is_file(), file)
                self.assertIn(f"class {cls}(", source(path), reference)

    def test_the_retry_correction_is_recorded_with_its_origin_and_destination(self) -> None:
        (correction,) = [item for item in self.trace["corrections"] if item["id"] == "COR4-01"]
        self.assertIn("retryable", correction["description"])
        self.assertIn("Extração 3", correction["origin"])
        files = {d["file"] for d in correction["agent_destination"]}
        for expected in ("core/stage_state.py", "core/orchestrator.py", "core/resume.py", "core/recheck_stage.py", "schemas/run-state.schema.json"):
            self.assertIn(expected, files)
        for destination in correction["agent_destination"]:
            self.assertTrue((REPO / destination["file"]).is_file(), destination["file"])
        self.assertTrue(correction["unchanged"], "o que NÃO mudou precisa estar dito")

    def test_deferred_and_rejected_items_say_why(self) -> None:
        self.assertTrue(self.trace["deferred"])
        for item in self.trace["deferred"]:
            self.assertTrue(item["reason"].strip() and item["planned_destination"].strip(), item)
        self.assertTrue(self.trace["rejected"])
        for item in self.trace["rejected"]:
            self.assertTrue(item["reason"].strip() and item["classification"] in CLASSES, item)

    def test_not_extracted_list_matches_the_scope_boundary(self) -> None:
        names = " ".join(self.trace["not_extracted"]).lower()
        for word in ("interface de revisão humana", "ingestão de resposta humana", "portal de perguntas", "dokuwiki", "daemon", "k4.3", "api de modelo",
                     "adaptador real de provedor", "analisador de telas", "persistência do backlog"):
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

    def test_the_item_extraction_3_deferred_for_routing_is_the_one_delivered_here(self) -> None:
        previous = load_json(REPO / "docs" / "extraction" / "EXTRACTION_003.json")
        deferred = [item for item in previous["deferred"] if "roteamento de revisão" in item["planned_destination"]]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["source_reference"]["symbols"], ["derive", "CLASSES"])
        covered = {symbol for c in self.trace["capabilities"] for symbol in c["source_reference"]["symbols"]}
        self.assertTrue({"derive"} <= covered)
        text = json.dumps(self.trace, ensure_ascii=False)
        self.assertIn("EX3", text, "a ligação com a extração anterior está registrada")


class DocumentationOfTheExtraction(Quiet, unittest.TestCase):
    DOC = REPO / "docs" / "AGENT_EXTRACTION_4.md"
    SECTIONS = [
        "Definição do roteamento de revisão", "Fontes estudadas", "Princípios genéricos extraídos", "Acoplamento do SYSCOM rejeitado",
        "Sinal de análise", "Classes de roteamento", "Precedência", "Criticidade e confiança", "Disponibilidade de evidência",
        "Impacto de afirmações", "Lacuna de fonte", "Decisão humana", "Amostragem de qualidade", "AUTO_PASS não é validação",
        "Contradições", "Privacidade, segurança e fronteira de dados", "Política como dado e versionamento", "Decisão de revisão",
        "Integração com AI_RECHECK", "Semântica de repetição", "Relação com a escada da Fundação", "Compatibilidade de runtime",
        "Limitações conhecidas", "Fora do escopo", "Rastreabilidade",
    ]

    def test_required_sections(self) -> None:
        text = source(self.DOC)
        for section in self.SECTIONS:
            self.assertRegex(text, rf"(?m)^## {re.escape(section)}$", section)

    def test_names_every_new_module_schema_policy_and_the_artifact(self) -> None:
        text = source(self.DOC)
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", text)
        for name in NEW_SCHEMAS:
            self.assertIn(f"schemas/{name}.schema.json", text)
        self.assertIn("policies/review-routing.policy.json", text)
        self.assertIn("docs/extraction/EXTRACTION_004.json", text)
        self.assertIn("tests/test_retryable_semantics.py", text)

    def test_documents_every_class_action_kind_family_source_type_and_reason_code(self) -> None:
        from core import analysis_signal, review_routing

        text = source(self.DOC)
        vocabulary = (
            analysis_signal.REVIEW_CLASSES + analysis_signal.ACTIONS + analysis_signal.DECISION_KINDS + analysis_signal.BASE_FAMILIES
            + analysis_signal.SOURCE_TYPES + analysis_signal.CRITICALITIES + analysis_signal.CONFIDENCE_BANDS + review_routing.WHY_CODES
            + review_routing.NEXT_ACTIONS + review_routing.RECHECK_STATUSES
        )
        for word in vocabulary:
            self.assertIn(word, text, word)
        for word in ("NON_RETRYABLE_FAILURE", "TECHNICAL_FAILURE", "REEVALUATED", "validation_effect", "NONE", "qualifier", "boundary_limit", "impact_class"):
            self.assertIn(word, text, word)

    def test_states_the_invariants_that_matter(self) -> None:
        text = source(self.DOC)
        for pattern in (
            r"(?i)SOURCE_GAP[^\n]*nunca[^\n]*HUMAN_DECISION|SOURCE_GAP não vira HUMAN_DECISION",
            r"(?i)AUTO_PASS[^\n]*não[^\n]*VALIDATED",
            r"(?i)baixa confiança não é revisão humana",
            r"(?i)alta confiança não é segurança",
            r"(?i)a classe nunca promove",
            r"(?i)a política não reordena",
            r"(?i)sem relógio",
            r"(?i)falha técnica[^\n]*não gasta",
        ):
            self.assertRegex(text, pattern)

    def test_states_the_runtime_facts_honestly(self) -> None:
        text = source(self.DOC)
        self.assertIn("3.15.0rc2", text)
        self.assertIn("3.10", text)
        self.assertRegex(text, r"(?i)linux|macos")
        self.assertRegex(text, r"(?i)não (foi|foram) (verificad|testad|executad)")

    def test_states_the_limits_that_matter(self) -> None:
        text = source(self.DOC).lower()
        for fragment in ("não há emissor de sinais", "não há portal", "não persiste", "calibr", "efeitos", "adaptador de domínio"):
            self.assertIn(fragment, text, fragment)

    def test_architecture_lists_the_new_core_modules_policy_and_the_extraction(self) -> None:
        text = source(REPO / "docs" / "ARCHITECTURE.md")
        for name in NEW_MODULES:
            self.assertIn(f"`{name}`", text)
        self.assertIn("AGENT_EXTRACTION_4.md", text)
        self.assertIn("review-routing.policy.json", text)

    def test_schema_readme_lists_the_new_contracts(self) -> None:
        text = source(REPO / "schemas" / "README.md")
        for name in NEW_SCHEMAS:
            self.assertIn(f"`{name}`", text)

    def test_the_main_readme_records_the_fourth_extraction(self) -> None:
        text = source(REPO / "README.md")
        self.assertIn("Agent Extraction 4", text)
        self.assertIn("EXTRACTION_004.json", text)

    def test_the_previous_extraction_doc_no_longer_claims_retryable_is_ignored(self) -> None:
        text = source(REPO / "docs" / "AGENT_EXTRACTION_3.md")
        self.assertIn("Extração 4", text)


if __name__ == "__main__":
    unittest.main()
