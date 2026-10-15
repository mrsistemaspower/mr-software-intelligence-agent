"""Linha de comando do núcleo."""
from __future__ import annotations

import contextlib
import io
import unittest

from tests.helpers import FIXTURE, TempDirCase, copy_pack, load_json, write_json
from core.__main__ import main


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


class Cli(TempDirCase):
    def test_no_arguments_prints_usage(self) -> None:
        code, out = run()
        self.assertEqual(code, 2)
        self.assertIn("validate-pack", out)

    def test_unknown_command(self) -> None:
        code, _ = run("bogus")
        self.assertEqual(code, 2)

    def test_validate_pack_requires_a_path(self) -> None:
        code, _ = run("validate-pack")
        self.assertEqual(code, 2)

    def test_check_schemas(self) -> None:
        code, out = run("check-schemas")
        self.assertEqual((code, out.strip()), (0, "OK    schemas"))

    def test_validate_packs_reports_each_pack(self) -> None:
        code, out = run("validate-packs")
        self.assertEqual(code, 0)
        for name in ("_template", "syscom", "acme-ledger"):
            self.assertIn(name, out)

    def test_check_isolation(self) -> None:
        code, out = run("check-isolation")
        self.assertEqual((code, out.strip()), (0, "OK    isolamento"))

    def test_validate_pack_success_and_failure(self) -> None:
        self.assertEqual(run("validate-pack", str(FIXTURE))[0], 0)
        broken = copy_pack(FIXTURE, self.tmp / "acme-ledger")
        facts = load_json(broken / "facts.json")
        facts["records"][2]["supported_by"].append("evd:nope")
        write_json(broken / "facts.json", facts)
        code, out = run("validate-pack", str(broken))
        self.assertEqual(code, 1)
        self.assertIn("FALHA", out)
        self.assertIn("P020", out)

    def test_validate_pack_on_a_missing_directory_fails(self) -> None:
        code, out = run("validate-pack", str(self.tmp / "nowhere"))
        self.assertEqual(code, 1)
        self.assertIn("P001", out)


if __name__ == "__main__":
    unittest.main()
