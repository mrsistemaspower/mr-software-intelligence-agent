"""Identidade determinística de artefatos derivados."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import json
import unittest
from unittest import mock

from tests.helpers import REPO  # noqa: F401  (garante o sys.path)
from core.hashing import Digest, hash_bytes
from core.identity import FORBIDDEN_PARAMETER_KEYS, ArtifactIdentity, artifact_identity
from core.paths import PathPortabilityError
from core.serialization import CanonicalJsonError
from core.snapshot import ArtifactEntry, build_snapshot

GOLDEN_ARTIFACT_ID = "art:doc.screen:sha256:ba8da291ee5da2f0d3545a7c5a367ff2b71da9281ec9668c8b3eb5bb977f382b"
GOLDEN_SNAPSHOT_ID = "snap:sha256:36750cd45702365d0456c8ac346fa13069586cf4b1d3a2381d9a214875bceb60"
ALPHA = hash_bytes(b"alpha")
BETA = hash_bytes(b"beta")


def snapshot(**overrides):
    entries = [
        ArtifactEntry.from_bytes("schemas/beta.json", "schema", b'{"b":1}'),
        ArtifactEntry.absent("layers/gamma.json", "layer"),
        ArtifactEntry.from_bytes("docs/alpha.txt", "doc", b"alpha"),
    ]
    options = dict(created_from_kind="declared-list", created_from_label="golden", software_id="zeta-erp")
    options.update(overrides)
    return build_snapshot(entries, **options)


def identity(**overrides) -> ArtifactIdentity:
    options = dict(
        inputs={"docs/alpha.txt": ALPHA},
        parameters={"depth": 2, "tags": ["a", "b"], "ratio": 0.5},
        snapshot=snapshot(),
    )
    options.update(overrides)
    return artifact_identity("doc.screen", "1", **options)


class FrozenContract(unittest.TestCase):
    def test_golden_id(self) -> None:
        self.assertEqual(identity().artifact_id, GOLDEN_ARTIFACT_ID)
        self.assertEqual(str(identity()), GOLDEN_ARTIFACT_ID)

    def test_recomputed_from_first_principles_with_only_hashlib_and_json(self) -> None:
        payload = {
            "kind": "artifact-identity", "identity_version": 1, "artifact_type": "doc.screen", "schema_version": "1",
            "inputs": {"docs/alpha.txt": "sha256:" + hashlib.sha256(b"alpha").hexdigest()},
            "parameters": {"depth": 2, "tags": ["a", "b"], "ratio": 0.5},
            "software_id": "zeta-erp", "snapshot_id": GOLDEN_SNAPSHOT_ID,
        }
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(identity().artifact_id, f"art:doc.screen:sha256:{digest}")
        self.assertEqual(identity().digest.digest, digest)
        self.assertEqual(identity().payload, payload)

    def test_the_api_has_no_argument_for_time_chance_machine_path_or_provider(self) -> None:
        names = set(inspect.signature(artifact_identity).parameters)
        self.assertEqual(names, {"artifact_type", "schema_version", "inputs", "parameters", "snapshot", "software_id"})
        self.assertEqual(set(identity().payload), {"kind", "identity_version", "artifact_type", "schema_version", "inputs",
                                                   "parameters", "software_id", "snapshot_id"})


class Determinism(unittest.TestCase):
    def test_same_inputs_same_id(self) -> None:
        self.assertEqual({identity().artifact_id for _ in range(5)}, {GOLDEN_ARTIFACT_ID})

    def test_dictionary_order_is_irrelevant(self) -> None:
        a = identity(inputs={"b": ALPHA, "a": BETA}, parameters={"x": 1, "y": {"q": 1, "p": 2}})
        b = identity(inputs={"a": BETA, "b": ALPHA}, parameters={"y": {"p": 2, "q": 1}, "x": 1})
        self.assertEqual(a.artifact_id, b.artifact_id)

    def test_list_order_inside_parameters_is_significant(self) -> None:
        self.assertNotEqual(identity(parameters={"t": [1, 2]}).artifact_id, identity(parameters={"t": [2, 1]}).artifact_id)

    def test_input_hash_forms_are_equivalent(self) -> None:
        forms = [ALPHA, f"sha256:{ALPHA.digest}", ALPHA.digest]
        ids = {identity(inputs={"docs/alpha.txt": form}).artifact_id for form in forms}
        self.assertEqual(ids, {GOLDEN_ARTIFACT_ID})

    def test_snapshot_object_and_snapshot_id_with_explicit_software_agree(self) -> None:
        by_object = identity(snapshot=snapshot())
        by_id = identity(snapshot=GOLDEN_SNAPSHOT_ID, software_id="zeta-erp")
        self.assertEqual(by_object.artifact_id, by_id.artifact_id)

    def test_unicode_is_literal_and_not_normalized(self) -> None:
        nfc = identity(parameters={"nome": "caf\u00e9"})
        nfd = identity(parameters={"nome": "cafe\u0301"})
        self.assertNotEqual(nfc.artifact_id, nfd.artifact_id)
        self.assertEqual(nfc.artifact_id, identity(parameters={"nome": "caf\u00e9"}).artifact_id)

    def test_no_clock_machine_or_chance_is_consulted(self) -> None:
        def forbidden(*_a, **_k):
            raise AssertionError("a identidade não pode consultar relógio, máquina ou acaso")

        with mock.patch("time.time", forbidden), mock.patch("time.time_ns", forbidden), mock.patch("uuid.uuid4", forbidden), \
                mock.patch("random.random", forbidden), mock.patch("socket.gethostname", forbidden), mock.patch("platform.node", forbidden):
            self.assertEqual(identity().artifact_id, GOLDEN_ARTIFACT_ID)

    def test_snapshot_generation_metadata_does_not_leak_into_the_artifact_id(self) -> None:
        later = snapshot(created_at="2031-01-01T00:00:00Z", notes="outra rodada", generator="outro/2.0",
                         created_from_kind="pack", created_from_label="origem diferente")
        self.assertEqual(identity(snapshot=later).artifact_id, GOLDEN_ARTIFACT_ID)

    def test_provider_model_timestamp_and_host_belong_next_to_the_artifact_not_inside_its_identity(self) -> None:
        envelope_a = {"artifact_id": identity().artifact_id, "generation": {"provider": "um", "model": "m1", "created_at": "2026-01-01T00:00:00Z", "host": "pc-a"}}
        envelope_b = {"artifact_id": identity().artifact_id, "generation": {"provider": "dois", "model": "m2", "created_at": "2031-06-06T06:06:06Z", "host": "pc-b"}}
        self.assertEqual(envelope_a["artifact_id"], envelope_b["artifact_id"])
        self.assertNotEqual(envelope_a, envelope_b)


class EveryInputMatters(unittest.TestCase):
    def test_changing_any_identity_input_changes_the_id(self) -> None:
        other_snapshot = build_snapshot([ArtifactEntry.absent("a.json", "layer")], created_from_kind="declared-list",
                                        created_from_label="x", software_id="zeta-erp")
        variants = {
            "type": artifact_identity("doc.other", "1", inputs={"docs/alpha.txt": ALPHA}, parameters={"depth": 2, "tags": ["a", "b"], "ratio": 0.5}, snapshot=snapshot()),
            "schema_version": artifact_identity("doc.screen", "2", inputs={"docs/alpha.txt": ALPHA}, parameters={"depth": 2, "tags": ["a", "b"], "ratio": 0.5}, snapshot=snapshot()),
            "input_hash": identity(inputs={"docs/alpha.txt": BETA}),
            "input_name": identity(inputs={"docs/other.txt": ALPHA}),
            "input_added": identity(inputs={"docs/alpha.txt": ALPHA, "docs/extra.txt": BETA}),
            "input_removed": identity(inputs={}),
            "param_value": identity(parameters={"depth": 3, "tags": ["a", "b"], "ratio": 0.5}),
            "param_added": identity(parameters={"depth": 2, "tags": ["a", "b"], "ratio": 0.5, "novo": True}),
            "param_removed": identity(parameters={"depth": 2, "tags": ["a", "b"]}),
            "param_type": identity(parameters={"depth": 2.0, "tags": ["a", "b"], "ratio": 0.5}),
            "snapshot": identity(snapshot=other_snapshot),
            "snapshot_removed": identity(snapshot=None, software_id="zeta-erp"),
            "software": identity(snapshot=GOLDEN_SNAPSHOT_ID, software_id="omega-crm"),
            "software_removed": identity(snapshot=GOLDEN_SNAPSHOT_ID),
        }
        ids = {name: value.artifact_id for name, value in variants.items()}
        self.assertNotIn(GOLDEN_ARTIFACT_ID, ids.values())
        self.assertEqual(len(set(ids.values())), len(ids), ids)

    def test_a_changed_source_byte_changes_the_artifact(self) -> None:
        data = bytearray(b"conteudo da fonte consumida")
        original = artifact_identity("doc.screen", "1", inputs={"fonte": hash_bytes(bytes(data))}).artifact_id
        seen = {original}
        for index in range(len(data)):
            mutated = bytearray(data)
            mutated[index] ^= 0x01
            seen.add(artifact_identity("doc.screen", "1", inputs={"fonte": hash_bytes(bytes(mutated))}).artifact_id)
        self.assertEqual(len(seen), 1 + len(data))

    def test_same_content_in_two_softwares_has_two_identities(self) -> None:
        a = artifact_identity("doc.screen", "1", inputs={"f": ALPHA}, software_id="zeta-erp")
        b = artifact_identity("doc.screen", "1", inputs={"f": ALPHA}, software_id="omega-crm")
        self.assertNotEqual(a.artifact_id, b.artifact_id)

    def test_int_and_float_parameters_are_different_values(self) -> None:
        self.assertNotEqual(identity(parameters={"n": 1}).artifact_id, identity(parameters={"n": 1.0}).artifact_id)


class GuardsAgainstAccidentalLeaks(unittest.TestCase):
    def test_time_chance_machine_and_provider_keys_are_refused_at_any_depth(self) -> None:
        for key in sorted(FORBIDDEN_PARAMETER_KEYS) + ["Timestamp", "CREATED_AT", "UUID", "HostName", "LLM_Provider"]:
            for build in (lambda k=key: {k: "x"}, lambda k=key: {"ok": {"inner": {k: 1}}}, lambda k=key: {"ok": [{"x": 1}, {k: 2}]}):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    identity(parameters=build())

    def test_the_guard_list_covers_the_enumerated_risks(self) -> None:
        for needed in ("timestamp", "created_at", "uuid", "hostname", "machine_name", "provider", "llm_provider", "llm_model"):
            self.assertIn(needed, FORBIDDEN_PARAMETER_KEYS)

    def test_absolute_paths_in_parameters_inputs_or_keys_are_refused(self) -> None:
        bad_values = ["C:\\MR-AI\\x", "c:/users/x", "\\\\srv\\share\\x", "/home/user/x", "/Users/me/x", "file:///etc/hosts", "~/docs"]
        for bad in bad_values:
            with self.subTest(bad=bad):
                with self.assertRaises(PathPortabilityError):
                    identity(parameters={"arquivo": bad})
                with self.assertRaises(PathPortabilityError):
                    identity(parameters={"lista": ["ok", {"fundo": bad}]})
                with self.assertRaises(PathPortabilityError):
                    identity(parameters={bad: 1})
                with self.assertRaises(PathPortabilityError):
                    identity(inputs={bad: ALPHA})

    def test_the_error_does_not_echo_the_path(self) -> None:
        with self.assertRaises(PathPortabilityError) as ctx:
            identity(parameters={"arquivo": "C:\\MR-AI\\segredo.txt"})
        self.assertNotIn("segredo", str(ctx.exception))

    def test_logical_paths_are_fine(self) -> None:
        identity(parameters={"arquivo": "docs/a/b.txt", "outro": "relativo\\estilo.windows"}, inputs={"evidence/one.png": ALPHA})

    def test_identity_is_the_same_whatever_machine_path_the_data_came_from(self) -> None:
        """Dois ambientes locais diferentes entregam o mesmo caminho lógico e o mesmo hash: mesmo id."""
        from core.paths import to_logical_path
        import tempfile
        from pathlib import Path

        ids = set()
        for _ in range(2):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / ("nivel-" + "x" * 5)
                (root / "docs").mkdir(parents=True)
                (root / "docs" / "alpha.txt").write_bytes(b"alpha")
                logical = to_logical_path(root / "docs" / "alpha.txt", root)
                ids.add(artifact_identity("doc.screen", "1", inputs={logical: ALPHA}).artifact_id)
        self.assertEqual(len(ids), 1)


class Validation(unittest.TestCase):
    def test_artifact_type(self) -> None:
        for good in ("x", "doc.screen", "screen-model", "a1.b2-c3_d4", "a" * 64):
            artifact_identity(good, "1")
        for bad in ("", "Screen", "a b", "a..b", "-a", "a-", ".a", "a" * 65, "1a", "á", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifact_identity(bad, "1")  # type: ignore[arg-type]

    def test_schema_version(self) -> None:
        for good in ("1", "1.0.0", "2026.10", "v1", "1.0.0-rc.1+build5"):
            artifact_identity("x", good)
        for bad in ("", " 1", "1 ", "a b", "-1", "1/2", None, 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifact_identity("x", bad)  # type: ignore[arg-type]

    def test_software_id(self) -> None:
        artifact_identity("x", "1", software_id="zeta-erp")
        for bad in ("Zeta", "zeta_erp", "", "-a", 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifact_identity("x", "1", software_id=bad)  # type: ignore[arg-type]

    def test_inputs(self) -> None:
        for bad in ({"": ALPHA}, {"a": "nope"}, {"a": ALPHA.digest.upper()}, {"a": "md5:" + "a" * 32}, {"a": None}, {"a": 5}, {5: ALPHA}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifact_identity("x", "1", inputs=bad)  # type: ignore[arg-type]

    def test_parameters_must_be_canonical_json(self) -> None:
        for bad in ({"x": float("nan")}, {"x": {1, 2}}, {1: "a"}, {"x": b"bytes"}, {"x": "a\ud800"}):
            with self.subTest(bad=bad), self.assertRaises(CanonicalJsonError):
                artifact_identity("x", "1", parameters=bad)

    def test_parameters_must_be_a_mapping(self) -> None:
        for bad in ([], "x", 5, ("a", 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifact_identity("x", "1", parameters=bad)  # type: ignore[arg-type]

    def test_none_and_empty_are_the_same(self) -> None:
        self.assertEqual(artifact_identity("x", "1").artifact_id, artifact_identity("x", "1", inputs={}, parameters={}).artifact_id)
        self.assertEqual(artifact_identity("x", "1").artifact_id, artifact_identity("x", "1", inputs=None, parameters=None).artifact_id)

    def test_snapshot_argument(self) -> None:
        for bad in ("snap:sha256:short", "sha256:" + "a" * 64, 5, {"snapshot_id": GOLDEN_SNAPSHOT_ID}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                artifact_identity("x", "1", snapshot=bad)  # type: ignore[arg-type]

    def test_snapshot_of_another_software_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            artifact_identity("x", "1", snapshot=snapshot(), software_id="omega-crm")

    def test_software_is_inherited_from_the_snapshot(self) -> None:
        inherited = artifact_identity("x", "1", snapshot=snapshot())
        explicit = artifact_identity("x", "1", snapshot=snapshot(), software_id="zeta-erp")
        self.assertEqual(inherited.payload["software_id"], "zeta-erp")
        self.assertEqual(inherited.artifact_id, explicit.artifact_id)

    def test_snapshot_without_software_adds_none(self) -> None:
        self.assertNotIn("software_id", artifact_identity("x", "1", snapshot=snapshot(software_id=None)).payload)


class ResultObject(unittest.TestCase):
    def test_payload_is_decoupled_from_the_callers_dict(self) -> None:
        params = {"lista": [1, 2], "fundo": {"a": 1}}
        result = identity(parameters=params)
        before = copy.deepcopy(result.payload)
        params["lista"].append(3)
        params["fundo"]["a"] = 99
        self.assertEqual(result.payload, before)
        self.assertEqual(result.artifact_id, identity(parameters={"lista": [1, 2], "fundo": {"a": 1}}).artifact_id)

    def test_tuples_become_arrays_in_the_payload(self) -> None:
        result = identity(parameters={"t": (1, 2)})
        self.assertEqual(result.payload["parameters"]["t"], [1, 2])
        self.assertEqual(result.artifact_id, identity(parameters={"t": [1, 2]}).artifact_id)

    def test_tampered_result_is_detected_on_reconstruction(self) -> None:
        good = identity()
        with self.assertRaises(ValueError):
            dataclasses.replace(good, artifact_id="art:doc.screen:sha256:" + "0" * 64)
        forged = copy.deepcopy(good.payload)
        forged["parameters"]["depth"] = 3
        with self.assertRaises(ValueError):
            dataclasses.replace(good, payload=forged)
        with self.assertRaises(ValueError):
            dataclasses.replace(good, digest=Digest("sha256", "0" * 64, 1))

    def test_id_format(self) -> None:
        self.assertRegex(identity().artifact_id, r"^art:[a-z][a-z0-9._-]*:sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
