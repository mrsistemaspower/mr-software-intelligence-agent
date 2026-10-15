"""Extração 1: acoplamento, compatibilidade, determinismo entre execuções, paridade com a referência,
segundo software sintético e rastreabilidade."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import FIXTURE, REPO, TempDirCase, load_json
from core import isolation
from core.atomic import atomic_write_json
from core.hashing import hash_bytes, hash_canonical_json, hash_file
from core.identity import artifact_identity
from core.integrity import IntegrityError, MISSING, MUTATED
from core.paths import PathPortabilityError, _windows_extended, to_logical_path
from core.serialization import CanonicalJsonError, canonical_dumps, canonical_pretty_bytes
from core.snapshot import (
    SnapshotError,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_directory,
    verify_snapshot,
)

NEW_MODULES = ["serialization", "hashing", "paths", "atomic", "integrity", "snapshot", "identity"]
NEW_FILES = [REPO / "core" / f"{name}.py" for name in NEW_MODULES]
INTENDED_MINIMUM = (3, 10)
SYNTAX_AUDITED_DOWN_TO = (3, 9)


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class NoSyscomCouplingInTheNewCore(unittest.TestCase):
    """O núcleo novo não conhece o projeto de referência, nem seus nomes, datas, caminhos ou dados."""

    FORBIDDEN = re.compile(
        r"\b(syscom|mr-syscom|k3|k4|k3_4|k41|k42|firebird|dokuwiki|dolphins|mr-ai|mr_syscom)\b|k4[._]\d|standard_lookup|standard_code_field",
        re.IGNORECASE,
    )
    FIXED_DATE = re.compile(r"\b(19|20)\d\d-\d\d-\d\d\b")
    DRIVE_PATH = re.compile(r"[A-Za-z]:\\[A-Za-z]")

    def test_the_new_files_exist(self) -> None:
        for path in NEW_FILES + [REPO / "schemas" / "knowledge-snapshot.schema.json"]:
            self.assertTrue(path.is_file(), path.name)

    def test_no_reference_project_term_in_any_new_file(self) -> None:
        hits = []
        for path in NEW_FILES + [REPO / "schemas" / "knowledge-snapshot.schema.json"]:
            for number, line in enumerate(source(path).splitlines(), start=1):
                match = self.FORBIDDEN.search(line)
                if match:
                    hits.append(f"{path.name}:{number}: {match.group(0)}")
        self.assertEqual(hits, [])

    def test_the_term_scan_has_teeth(self) -> None:
        for sample in ("syscom", "SYSCOM", "mr-syscom", "k4", "K3", "k4.1", "k4_2", "Firebird", "C:\\MR-AI", "DokuWiki"):
            self.assertIsNotNone(self.FORBIDDEN.search(f"x {sample} y"), sample)
        for innocent in ("hashing", "backup4", "block3", "sha256", "tokens4all"):
            self.assertIsNone(self.FORBIDDEN.search(f"x {innocent} y"), innocent)

    def test_no_fixed_date_and_no_drive_path_literal(self) -> None:
        for path in NEW_FILES + [REPO / "schemas" / "knowledge-snapshot.schema.json"]:
            text = source(path)
            with self.subTest(file=path.name):
                self.assertIsNone(self.FIXED_DATE.search(text))
                self.assertIsNone(self.DRIVE_PATH.search(text))

    def test_every_leak_term_of_every_pack_is_absent_from_the_new_files(self) -> None:
        terms = sorted({t for info in isolation.collect_leak_terms(REPO).values() for t in info["terms"]})
        self.assertGreaterEqual(len(terms), 5)
        findings = isolation.scan_files(NEW_FILES + [REPO / "schemas" / "knowledge-snapshot.schema.json"], terms, "LEAK", REPO)
        self.assertEqual([str(f) for f in findings], [])

    def test_no_model_provider_term_in_the_new_files(self) -> None:
        findings = isolation.scan_files(
            NEW_FILES + [REPO / "schemas" / "knowledge-snapshot.schema.json"], isolation.load_provider_terms(REPO), "PROVIDER", REPO)
        self.assertEqual([str(f) for f in findings], [])

    def test_the_whole_repository_isolation_check_still_passes(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_all(REPO)], [])

    def test_windows_specific_logic_lives_in_one_module_only(self) -> None:
        pattern = re.compile(r"""["']nt["']|win32|msvcrt|winreg|ntpath|\\\\\\\\\?""")
        for path in NEW_FILES:
            hits = pattern.findall(source(path))
            with self.subTest(file=path.name):
                if path.name == "paths.py":
                    self.assertTrue(hits)
                else:
                    self.assertEqual(hits, [])

    def test_no_branch_on_software_identity_anywhere_in_the_core(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_core_independence(REPO)], [])


class DeterministicInputsOnly(unittest.TestCase):
    """Nenhum módulo novo importa ou chama algo que traga relógio, máquina, acaso ou ambiente para dentro do resultado."""

    ALLOWED_IMPORTS = {"__future__", "dataclasses", "datetime", "functools", "hashlib", "json", "math", "os", "pathlib", "re", "secrets", "typing"}
    BANNED_ATTRIBUTES = {
        "environ", "getenv", "getlogin", "gethostname", "node", "now", "utcnow", "today", "uuid1", "uuid4", "time", "time_ns",
        "monotonic", "perf_counter", "getpid", "getuid", "getcwd", "random", "randint", "urandom", "system", "popen", "Popen",
    }

    def imported(self, path: Path) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(ast.parse(source(path))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_only_the_standard_library_subset_is_imported(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertLessEqual(self.imported(path), self.ALLOWED_IMPORTS)

    def test_randomness_is_confined_to_temporary_file_names(self) -> None:
        users = [path.name for path in NEW_FILES if "secrets" in self.imported(path)]
        self.assertEqual(users, ["atomic.py"])

    def test_no_ambient_input_is_ever_read(self) -> None:
        for path in NEW_FILES:
            attributes = {node.attr for node in ast.walk(ast.parse(source(path))) if isinstance(node, ast.Attribute)}
            with self.subTest(file=path.name):
                self.assertEqual(attributes & self.BANNED_ATTRIBUTES, set())

    def test_the_ambient_scan_has_teeth(self) -> None:
        tree = ast.parse("import datetime, os\nx = datetime.datetime.now()\ny = os.environ['A']\n")
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes & self.BANNED_ATTRIBUTES, {"now", "environ"})

    def test_no_model_sdk_or_network_import(self) -> None:
        banned = {"anthropic", "openai", "requests", "urllib", "http", "socket", "ssl", "subprocess", "asyncio", "threading"}
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(self.imported(path) & banned, set())


class RuntimeCompatibility(unittest.TestCase):
    """Compatibilidade declarada: mínimo pretendido 3.10; sintaxe auditada até 3.9. Só o interpretador de hoje executa."""

    def test_syntax_is_valid_for_the_audited_floor(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                ast.parse(source(path), filename=path.name, feature_version=SYNTAX_AUDITED_DOWN_TO)

    def test_the_floor_check_has_teeth(self) -> None:
        with self.assertRaises(SyntaxError):
            ast.parse("match x:\n    case 1:\n        pass\n", feature_version=SYNTAX_AUDITED_DOWN_TO)

    def test_no_api_or_keyword_newer_than_the_audited_floor(self) -> None:
        newer = {"bit_count", "pairwise", "batched", "file_digest", "StrEnum", "ExceptionGroup", "TaskGroup", "tomllib", "Self", "override"}
        newer_keywords = {"strict", "slots", "kw_only", "match_args"}
        for path in NEW_FILES:
            tree = ast.parse(source(path))
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            keywords = {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords}
            with self.subTest(file=path.name):
                self.assertEqual(names & newer, set())
                self.assertEqual(keywords & newer_keywords, set())

    def test_annotations_are_lazy_so_modern_union_syntax_never_runs_on_older_pythons(self) -> None:
        for path in NEW_FILES:
            with self.subTest(file=path.name):
                self.assertIn("from __future__ import annotations", source(path))

    def test_declared_versions(self) -> None:
        self.assertLessEqual(SYNTAX_AUDITED_DOWN_TO, INTENDED_MINIMUM)
        self.assertGreaterEqual(sys.version_info[:2], INTENDED_MINIMUM)


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
            doc = ast.get_docstring(ast.parse(source(path)))
            with self.subTest(file=path.name):
                self.assertTrue(doc and len(doc) > 80)


COMPUTE = r'''
from core.hashing import hash_bytes, hash_canonical_json
from core.identity import artifact_identity
from core.serialization import canonical_dumps
from core.snapshot import ArtifactEntry, build_snapshot


def compute():
    value = {"z": [1, 2.5, None, True, "a\u00e7\u00e3o \u6f22\u5b57 \U0001F600"], "a": {"y": "\u00e9", "x": 10 ** 25}}
    entries = [
        ArtifactEntry.from_bytes("docs/b.txt", "doc", b"beta"),
        ArtifactEntry.absent("layers/x.json", "layer"),
        ArtifactEntry.from_bytes("docs/a.txt", "doc", b"alpha"),
    ]
    shot = build_snapshot(entries, created_from_kind="declared-list", created_from_label="sub", software_id="zeta-erp")
    ident = artifact_identity("doc.screen", "1", inputs={"docs/a.txt": hash_bytes(b"alpha")},
                              parameters={"k": [1, 2], "m": "\u00e7"}, snapshot=shot)
    return {"canon": canonical_dumps(value), "hash": str(hash_canonical_json(value)),
            "snap": shot.snapshot_id, "art": ident.artifact_id}
'''
SCRIPT = COMPUTE + "\nimport json\nprint(json.dumps(compute(), ensure_ascii=True, sort_keys=True))\n"


class DeterministicAcrossRuns(TempDirCase):
    """Mesmo resultado em outro processo, com outra semente de hash, outra codificação, outro diretório e outra máquina."""

    def run_script(self, **env_overrides: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONHASHSEED", "PYTHONUTF8", "PYTHONIOENCODING"}}
        env.update(PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
        env.update(env_overrides)
        workdir = self.tmp / f"cwd-{len(list(self.tmp.iterdir()))}"
        workdir.mkdir()
        result = subprocess.run([sys.executable, "-B", "-c", SCRIPT], cwd=workdir, env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("ascii"))

    def expected(self) -> dict:
        namespace: dict = {}
        exec(compile(COMPUTE, "compute", "exec"), namespace)
        return namespace["compute"]()

    def test_other_processes_agree_with_this_one_under_hostile_environments(self) -> None:
        reference = self.expected()
        environments = [
            {"PYTHONHASHSEED": "1"},
            {"PYTHONHASHSEED": "2", "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii"},
            {"PYTHONHASHSEED": "random", "PYTHONUTF8": "1", "LANG": "C", "LC_ALL": "C", "TZ": "Pacific/Kiritimati"},
            {"PYTHONHASHSEED": "31337", "COMPUTERNAME": "OUTRA-MAQUINA", "HOSTNAME": "outra-maquina", "USERNAME": "outra-pessoa", "USER": "outra-pessoa"},
        ]
        for overrides in environments:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.run_script(**overrides), reference)

    def test_values_have_the_documented_shapes_and_are_self_consistent(self) -> None:
        reference = self.expected()
        self.assertRegex(reference["snap"], r"^snap:sha256:[0-9a-f]{64}$")
        self.assertRegex(reference["art"], r"^art:doc\.screen:sha256:[0-9a-f]{64}$")
        self.assertEqual(reference["hash"], f"sha256:{hash_canonical_json(json.loads(reference['canon'])).digest}")


# ---------------------------------------------------------------- paridade com a referência
# Valores gerados UMA vez a partir de uma CÓPIA, fora do workspace de referência, dos dois arquivos estudados
# (execução com `py -B`, sem importar nada de dentro do workspace de referência). Os testes abaixo não
# dependem de que esse workspace exista.
FIXTURES = {
    "unsorted_keys": {"b": 1, "a": 2},
    "nested_mixed": {"z": [3, 2, 1], "a": {"y": None, "x": True}, "m": "ação"},
    "empty_containers": {"l": [], "o": {}, "s": ""},
    "numbers": [0, -1, 1.5, 0.1, 1e22, 10 ** 20, 123456789012345678901234567890],
    "escapes": {"k": "linha1\nlinha2\t\"aspas\"\\barra\u0001\u001f"},
    "unicode": {"emoji": "\U0001F600", "cjk": "漢字", "acentos": "çãõé", "nul": "a\u0000b"},
    "record_like": {"entries": [{"role": "doc", "path": "x/y.json", "sha256": "ab" * 32}], "version": 1},
}
REFERENCE_CANON = {
    "unsorted_keys": '{"a":2,"b":1}',
    "nested_mixed": '{"a":{"x":true,"y":null},"m":"ação","z":[3,2,1]}',
    "empty_containers": '{"l":[],"o":{},"s":""}',
    "numbers": "[0,-1,1.5,0.1,1e+22,100000000000000000000,123456789012345678901234567890]",
    "escapes": r'{"k":"linha1\nlinha2\t\"aspas\"\\barra\u0001\u001f"}',
    "unicode": r'{"acentos":"çãõé","cjk":"漢字","emoji":"😀","nul":"a\u0000b"}',
    "record_like": '{"entries":[{"path":"x/y.json","role":"doc","sha256":"' + "ab" * 32 + '"}],"version":1}',
}
REFERENCE_FINGERPRINT = {
    "unsorted_keys": "d3626ac30a87e6f7a6428233b3c68299976865fa5508e4267c5415c76af7a772",
    "nested_mixed": "fca01ca1c7c95eda0705689c61ebc584df0c73f72a61ba5e5652f50bad05cc61",
    "empty_containers": "5e4fa172ee0393faff0de8bef28f78089d7afd9a089c54b4402318d087cc2a50",
    "numbers": "a9c13f69a5b877744cc56d9df30d2b9c31c40bfd13165292d4f9dcc50617c520",
    "escapes": "bdac55411dc988250165977626c3fea887c655d6ae835c57e616dc57a9eadfa4",
    "unicode": "98e7b4ecc29d1accab560f24428229683bd98648ef28bf286ff4b8556382401b",
    "record_like": "6c60d448c5961f95ced0ab8e6ce3e07f530cb4f50448a755c49c15ea4d4bebcf",
}
SORTED_FIXTURES = {
    "sorted_flat": {"a": 1, "b": [1, 2], "c": {"d": "e"}},
    "sorted_unicode": {"a": "ação", "b": {"c": None}},
}
REFERENCE_PRETTY = {
    "sorted_flat": '{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ],\n  "c": {\n    "d": "e"\n  }\n}\n',
    "sorted_unicode": '{\n  "a": "ação",\n  "b": {\n    "c": null\n  }\n}\n',
}
REFERENCE_BYTES = {
    b"": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    b"abc": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    bytes(range(256)): "40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880",
}
REFERENCE_FILES = {
    0: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    1: "ca358758f6d27e6cf45272937977a748fd88391db679ceda7dc7bf1f005ee879",
    1048575: "4ddf525d0f1482ae9ed8ff016e61988dfab2707743585ac310be210689ddb8d6",
    1048576: "06b7bbfb7824aa03382051691630eb26de85102d1b08a81e907ec0744cd8a286",
    1048577: "0d09f3eabb5c78e5d435c41e3b3755f1c4d038657f739d0be0083c24e71235df",
    3145735: "1e3ecbe93dd78dcabe8bde02bb9e5ae74efa8fbfa170b7fd87db91c48b4cb891",
}
REFERENCE_WRITE_VERDICTS = ["CREATED", "NO_CHANGE", "UPDATED"]
REFERENCE_FINAL_FILE_SHA = "6fbe364a6ced4087dc15f0cdc0ba438681a56dfc3adfc0a15f2f2464d13b352d"


def blob(size: int) -> bytes:
    return bytes((i * 31 + 7) % 256 for i in range(size))


class ParityWithTheReferenceBehaviour(TempDirCase):
    def test_canonical_text_is_identical_to_the_reference_for_every_fixture(self) -> None:
        for name, value in FIXTURES.items():
            with self.subTest(name):
                self.assertEqual(canonical_dumps(value), REFERENCE_CANON[name])

    def test_fingerprints_are_identical_to_the_reference(self) -> None:
        for name, value in FIXTURES.items():
            with self.subTest(name):
                self.assertEqual(hash_canonical_json(value).digest, REFERENCE_FINGERPRINT[name])

    def test_reference_fingerprint_is_sha256_of_the_utf8_of_its_canonical_text(self) -> None:
        import hashlib

        for name, text in REFERENCE_CANON.items():
            with self.subTest(name):
                self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), REFERENCE_FINGERPRINT[name])

    def test_readable_form_is_identical_to_the_reference_when_keys_were_inserted_in_order(self) -> None:
        for name, value in SORTED_FIXTURES.items():
            with self.subTest(name):
                self.assertEqual(canonical_pretty_bytes(value).decode("utf-8"), REFERENCE_PRETTY[name])

    def test_byte_digests_are_identical_to_the_reference(self) -> None:
        for data, expected in REFERENCE_BYTES.items():
            self.assertEqual(hash_bytes(data).digest, expected)

    def test_file_digests_are_identical_to_the_reference_across_chunk_boundaries(self) -> None:
        for size, expected in REFERENCE_FILES.items():
            path = self.tmp / f"f{size}.bin"
            path.write_bytes(blob(size))
            with self.subTest(size=size):
                self.assertEqual(hash_file(path).digest, expected)

    def test_idempotent_write_verdicts_and_final_bytes_are_identical_to_the_reference(self) -> None:
        target = self.tmp / "sub" / "w.json"
        verdicts = [atomic_write_json(target, SORTED_FIXTURES["sorted_flat"]).status,
                    atomic_write_json(target, SORTED_FIXTURES["sorted_flat"]).status,
                    atomic_write_json(target, SORTED_FIXTURES["sorted_unicode"]).status]
        self.assertEqual(verdicts, REFERENCE_WRITE_VERDICTS)
        self.assertEqual(hash_file(target).digest, REFERENCE_FINAL_FILE_SHA)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["w.json"])

    def test_arrays_and_tuples_agree_with_the_reference(self) -> None:
        self.assertEqual(canonical_dumps((1, 2)), canonical_dumps([1, 2]))

    def test_relative_path_inside_the_root_agrees_with_the_reference(self) -> None:
        (self.tmp / "tools" / "batch").mkdir(parents=True)
        self.assertEqual(to_logical_path(self.tmp / "tools" / "batch" / "x.py", self.tmp), "tools/batch/x.py")

    def test_long_path_prefix_agrees_with_the_reference_where_the_reference_was_correct(self) -> None:
        long_path = "C:\\" + "\\".join(f"segmento{i}" for i in range(1, 40)) + "\\arquivo.txt"
        self.assertEqual(_windows_extended(long_path), "\\\\?\\" + long_path)
        unc = "\\\\servidor\\compartilhamento\\" + "\\".join(f"segmento{i}" for i in range(1, 40)) + "\\a.txt"
        self.assertEqual(_windows_extended(unc), "\\\\?\\UNC\\" + unc[2:])

    # ---- divergências deliberadas: o que a referência fazia por acidente e o núcleo novo recusa
    def test_deliberate_divergence_non_finite_numbers(self) -> None:
        # Referência: 'NaN' e 'Infinity' saíam no texto, que não é JSON válido.
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(CanonicalJsonError):
                canonical_dumps(bad)

    def test_deliberate_divergence_non_string_keys_can_no_longer_collide(self) -> None:
        # Referência: {1: 'a'}, {True: 'a'} e {'1': 'a'} davam o mesmo texto ou 'true'; chaves mistas davam TypeError.
        self.assertEqual(canonical_dumps({"1": "a"}), '{"1":"a"}')
        for bad in ({1: "a"}, {True: "a"}, {None: "a"}, {1: "a", "1": "b"}):
            with self.assertRaises(CanonicalJsonError):
                canonical_dumps(bad)

    def test_deliberate_divergence_lone_surrogate_fails_at_the_boundary_with_a_clear_error(self) -> None:
        # Referência: o texto saía e o erro (UnicodeEncodeError) só aparecia na hora de codificar.
        with self.assertRaises(CanonicalJsonError):
            canonical_dumps("\ud800")

    def test_deliberate_divergence_path_outside_the_root_never_becomes_an_absolute_string(self) -> None:
        # Referência: caía para o caminho absoluto quando o arquivo estava fora da raiz.
        root = self.tmp / "root"
        root.mkdir()
        with self.assertRaises(PathPortabilityError):
            to_logical_path(self.tmp / "fora.txt", root)

    def test_deliberate_divergence_short_windows_paths_are_not_prefixed(self) -> None:
        # Referência: prefixava sempre, mudando a semântica de caminhos curtos (e malformando caminhos UNC).
        self.assertEqual(_windows_extended("C:\\pasta\\a.txt"), "C:\\pasta\\a.txt")


# ---------------------------------------------------------------- segundo software sintético
def build_workspace(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def run_pipeline(workspace: Path, scratch: Path, software_id: str, specs: list[tuple[str, str]]) -> dict:
    """O mesmo código para qualquer software: snapshot, gravação, recarga, verificação, identidade, artefato."""
    shot = snapshot_directory(workspace, specs, label=f"workspace de {software_id}", software_id=software_id)
    state = scratch / f"{software_id}.snapshot.json"
    save_snapshot(state, shot)
    loaded = load_snapshot(state)
    verify_snapshot(loaded, workspace).raise_if_failed()
    inputs = {e.path: e.sha256 for e in loaded.entries if e.status == "PRESENT"}
    identity = artifact_identity("derived.view", "1", inputs=inputs, snapshot=loaded, parameters={"nivel": 1})
    atomic_write_json(scratch / f"{software_id}.artifact.json", {"id": identity.artifact_id, "snapshot": loaded.snapshot_id})
    return {"snapshot_id": loaded.snapshot_id, "artifact_id": identity.artifact_id, "entries": len(loaded.entries), "loaded_equals": loaded == shot}


class SecondSyntheticSoftware(TempDirCase):
    ZETA = {"docs/guide.md": b"# Guia Zeta\n", "data/rows.json": b'{"rows":[1,2,3]}', "shots/a.bin": bytes(range(50))}
    OMEGA = {"manual/intro.txt": "Introdução Ômega\n".encode("utf-8"), "db/schema.sql": b"create table t(a int);\n"}
    ZETA_SPECS = [("docs/guide.md", "doc"), ("data/rows.json", "records"), ("shots/a.bin", "capture"), ("layers/human.json", "layer")]
    OMEGA_SPECS = [("manual/intro.txt", "doc"), ("db/schema.sql", "schema"), ("layers/human.json", "layer")]

    def setUp(self) -> None:
        super().setUp()
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.zeta = self.tmp / "zeta"
        self.omega = self.tmp / "omega"
        build_workspace(self.zeta, self.ZETA)
        build_workspace(self.omega, self.OMEGA)

    def test_the_same_primitives_serve_two_unrelated_softwares(self) -> None:
        a = run_pipeline(self.zeta, self.scratch, "zeta-erp", self.ZETA_SPECS)
        b = run_pipeline(self.omega, self.scratch, "omega-crm", self.OMEGA_SPECS)
        self.assertTrue(a["loaded_equals"] and b["loaded_equals"])
        self.assertEqual((a["entries"], b["entries"]), (4, 3))
        self.assertNotEqual(a["snapshot_id"], b["snapshot_id"])
        self.assertNotEqual(a["artifact_id"], b["artifact_id"])

    def test_a_pipeline_run_is_repeatable_and_location_independent(self) -> None:
        first = run_pipeline(self.zeta, self.scratch, "zeta-erp", self.ZETA_SPECS)
        moved = self.tmp / "elsewhere" / "deeper" / "zeta-moved"
        shutil.copytree(self.zeta, moved)
        scratch_two = self.tmp / "scratch2"
        scratch_two.mkdir()
        second = run_pipeline(moved, scratch_two, "zeta-erp", self.ZETA_SPECS)
        self.assertEqual(first, second)

    def test_identical_content_in_two_softwares_still_has_two_identities(self) -> None:
        twin = self.tmp / "twin"
        build_workspace(twin, self.ZETA)
        a = snapshot_directory(self.zeta, self.ZETA_SPECS, label="a", software_id="zeta-erp")
        b = snapshot_directory(twin, self.ZETA_SPECS, label="b", software_id="omega-crm")
        self.assertEqual(a.artifacts_digest, b.artifacts_digest)
        self.assertNotEqual(a.snapshot_id, b.snapshot_id)
        with self.assertRaises(SnapshotError):
            diff_snapshots(a, b)

    def test_each_software_is_verified_against_its_own_workspace_only(self) -> None:
        a = snapshot_directory(self.zeta, self.ZETA_SPECS, label="a", software_id="zeta-erp")
        b = snapshot_directory(self.omega, self.OMEGA_SPECS, label="b", software_id="omega-crm")
        self.assertTrue(verify_snapshot(a, self.zeta).ok)
        self.assertTrue(verify_snapshot(b, self.omega).ok)
        crossed = verify_snapshot(a, self.omega)
        self.assertFalse(crossed.ok)
        self.assertEqual({f.kind for f in crossed.findings}, {MISSING})
        (self.omega / "db" / "schema.sql").write_bytes(b"alter table t add b int;\n")
        self.assertTrue(verify_snapshot(a, self.zeta).ok, "mutação em um software não afeta o outro")
        self.assertEqual([(f.kind, f.path) for f in verify_snapshot(b, self.omega).findings], [(MUTATED, "db/schema.sql")])

    def test_frozen_evidence_gate_works_for_every_software(self) -> None:
        for root, specs, sid in ((self.zeta, self.ZETA_SPECS, "zeta-erp"), (self.omega, self.OMEGA_SPECS, "omega-crm")):
            shot = snapshot_directory(root, specs, label=sid, software_id=sid)
            first_file = next(root / e.path for e in shot.entries if e.status == "PRESENT")
            original = first_file.read_bytes()
            first_file.write_bytes(original + b"!")
            with self.subTest(sid), self.assertRaises(IntegrityError):
                verify_snapshot(shot, root).raise_if_failed()
            first_file.write_bytes(original)
            verify_snapshot(shot, root).raise_if_failed()

    def test_the_foundation_synthetic_pack_is_a_third_software_through_the_same_code(self) -> None:
        specs = [("manifest.json", "manifest"), ("software.json", "metadata"), ("sources.json", "records"),
                 ("facts.json", "records"), ("layers/not-yet.json", "layer")]
        shot = snapshot_directory(FIXTURE, specs, label="pacote sintético da Foundation", software_id="acme-ledger")
        self.assertEqual(sum(e.status == "PRESENT" for e in shot.entries), 4)
        self.assertTrue(verify_snapshot(shot, FIXTURE).ok)
        copy = self.tmp / "fixture-copy"
        shutil.copytree(FIXTURE, copy)
        self.assertEqual(snapshot_directory(copy, specs, label="outra", software_id="acme-ledger").snapshot_id, shot.snapshot_id)
        from core.paths import find_local_paths

        self.assertEqual(find_local_paths(shot.to_dict()), [])
        self.assertNotIn(REPO.parent.name, json.dumps(shot.to_dict(), ensure_ascii=False))

    def test_the_foundation_template_instantiated_as_a_new_software_is_handled_too(self) -> None:
        from tests.helpers import TEMPLATE, copy_pack

        pack = copy_pack(TEMPLATE, self.tmp / "novo-software")
        specs = [("manifest.json", "manifest"), ("software.json", "metadata")]
        shot = snapshot_directory(pack, specs, label="template", software_id="novo-software")
        self.assertEqual(len(shot.entries), 2)
        self.assertTrue(verify_snapshot(shot, pack).ok)


# ---------------------------------------------------------------- rastreabilidade
TRACE = REPO / "docs" / "extraction" / "EXTRACTION_001.json"
CLASSES = {"GENERIC_BEHAVIOR", "SYSCOM_COUPLING", "DATASET_SPECIFIC", "HISTORICAL_ACCIDENT"}
CAPABILITY_FIELDS = {"id", "capability", "source_reference", "source_classification", "agent_destination", "adaptation", "rejected_coupling", "tests"}


class TraceabilityArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.trace = load_json(TRACE)

    def test_header(self) -> None:
        trace = self.trace
        self.assertEqual(trace["extraction"], "EXTRACTION_001")
        self.assertEqual(trace["foundation_commit"], "fdb4fc52cb9329bea21b5048ca9ecede1aa6fd08")
        reference = trace["reference"]
        self.assertEqual(reference["access"], "read-only")
        self.assertRegex(reference["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(len(reference["sources"]), 2)
        for item in reference["sources"]:
            self.assertRegex(item["git_blob"], r"^[0-9a-f]{40}$")

    def test_every_capability_has_every_required_field(self) -> None:
        self.assertGreaterEqual(len(self.trace["capabilities"]), 8)
        for capability in self.trace["capabilities"]:
            with self.subTest(capability["id"]):
                self.assertTrue(CAPABILITY_FIELDS <= set(capability), CAPABILITY_FIELDS - set(capability))
                for field in ("capability", "adaptation"):
                    self.assertTrue(capability[field].strip())
                self.assertTrue(capability["rejected_coupling"], "toda extração rejeita algum acoplamento")
                self.assertTrue(capability["tests"])
                self.assertTrue(set(capability["source_classification"]) <= CLASSES)
                self.assertTrue(capability["source_classification"])
                self.assertTrue(capability["source_reference"]["file"] and capability["source_reference"]["symbols"])

    def test_ids_are_unique_and_ordered(self) -> None:
        ids = [c["id"] for c in self.trace["capabilities"]]
        self.assertEqual(ids, sorted(set(ids)))

    def test_source_references_point_at_the_two_studied_files(self) -> None:
        studied = {item["path"] for item in self.trace["reference"]["sources"]}
        every_reference = []
        for capability in self.trace["capabilities"]:
            every_reference.append(capability["source_reference"])
            every_reference.extend(capability["source_reference"].get("also", []))
        for item in self.trace["deferred"] + self.trace["rejected"]:
            every_reference.append(item["source_reference"])
        for reference in every_reference:
            with self.subTest(reference["file"], symbols=reference["symbols"]):
                self.assertIn(reference["file"], studied)
                self.assertTrue(reference["symbols"])

    def test_destinations_exist_and_define_what_they_claim(self) -> None:
        for capability in self.trace["capabilities"]:
            for destination in capability["agent_destination"]:
                path = REPO / destination["file"]
                with self.subTest(capability["id"], destination=destination["file"]):
                    self.assertTrue(path.is_file())
                    if path.suffix == ".py":
                        tree = ast.parse(source(path))
                        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
                        defined |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
                        for symbol in destination["symbols"]:
                            self.assertIn(symbol, defined)

    def test_every_new_core_module_and_the_schema_are_traced_to_a_capability(self) -> None:
        destinations = {d["file"] for c in self.trace["capabilities"] for d in c["agent_destination"]}
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", destinations)
        self.assertIn("schemas/knowledge-snapshot.schema.json", destinations)

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
            self.assertTrue(item["reason"].strip() and item["planned_destination"].strip() and item["source_reference"]["symbols"], item)
        self.assertTrue(self.trace["rejected"])
        for item in self.trace["rejected"]:
            self.assertTrue(item["reason"].strip() and item["classification"] in CLASSES and item["source_reference"]["symbols"], item)

    def test_the_artifact_is_portable(self) -> None:
        text = TRACE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\", text), "sem caminho de unidade do Windows")
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9.])/(home|Users|mnt|var|usr|opt)/", text))
        self.assertNotIn(REPO.parent.name, text)
        from core.paths import find_local_paths

        self.assertEqual(find_local_paths(self.trace), [])

    def test_the_artifact_is_canonical_pretty_json(self) -> None:
        raw = TRACE.read_bytes()
        self.assertEqual(raw, canonical_pretty_bytes(json.loads(raw.decode("utf-8"))))


class DocumentationOfTheExtraction(unittest.TestCase):
    DOC = REPO / "docs" / "AGENT_EXTRACTION_1.md"
    SECTIONS = [
        "Fontes estudadas", "Invariantes genéricos extraídos", "Acoplamento do SYSCOM rejeitado", "Componentes novos do Agent Core",
        "Garantias de determinismo", "Semântica do snapshot", "Garantias de integridade", "Compatibilidade de runtime",
        "Limitações conhecidas", "Rastreabilidade",
    ]

    def test_required_sections(self) -> None:
        text = source(self.DOC)
        for section in self.SECTIONS:
            self.assertRegex(text, rf"(?m)^## {re.escape(section)}$", section)

    def test_names_every_new_module_and_the_contract(self) -> None:
        text = source(self.DOC)
        for name in NEW_MODULES:
            self.assertIn(f"core/{name}.py", text)
        self.assertIn("mr-canonical-json/1", text)
        self.assertIn("schemas/knowledge-snapshot.schema.json", text)
        self.assertIn("docs/extraction/EXTRACTION_001.json", text)

    def test_does_not_claim_rfc_8785_compatibility(self) -> None:
        text = source(self.DOC)
        self.assertIn("RFC 8785", text)
        self.assertNotRegex(text, r"(?i)\b(é|são|esta|está)\s+(compatível|conforme)\s+(com\s+)?(o\s+)?RFC 8785")

    def test_states_the_runtime_facts_honestly(self) -> None:
        text = source(self.DOC)
        self.assertIn("3.15.0rc2", text)
        self.assertIn("3.10", text)
        self.assertRegex(text, r"(?i)linux|macos")
        self.assertRegex(text, r"(?i)não (foi|foram) (verificad|testad|executad)")

    def test_architecture_lists_the_new_core_modules(self) -> None:
        text = source(REPO / "docs" / "ARCHITECTURE.md")
        for name in NEW_MODULES:
            self.assertIn(f"`{name}`", text)


if __name__ == "__main__":
    unittest.main()
