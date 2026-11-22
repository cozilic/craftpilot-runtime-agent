"""Kontrollerar att Dockerfile kopierar varje lokal modul agenten importerar.

En glömd modul syns inte i testsviten annars: agenten importerar den ur
källkatalogen, men imagen innehåller bara filerna Dockerfile räknar upp och
kraschar först vid start på noden med `ModuleNotFoundError` (se #7, där
`network_stats.py` saknades). Testet är Docker-fritt och läser bara filer.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "craftpilot_runtime_agent.py"
DOCKERFILE = ROOT / "Dockerfile"


def local_imports(path: Path) -> set[str]:
    """Moduler som filen importerar och som finns som .py bredvid den."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {name for name in names if (ROOT / f"{name}.py").is_file()}


def imported_modules(entrypoint: Path = ENTRYPOINT) -> set[str]:
    """Alla lokala moduler i entrypointens importkedja, inte bara dess egna.

    Även en modul som bara importeras av en annan lokal modul måste finnas i
    imagen, så kedjan följs hela vägen ned.
    """
    seen: set[str] = set()
    pending = [entrypoint]
    while pending:
        path = pending.pop()
        if path.stem in seen:
            continue
        seen.add(path.stem)
        pending.extend(ROOT / f"{name}.py" for name in local_imports(path) if name not in seen)
    return seen - {entrypoint.stem}


def copied_files(dockerfile: str) -> set[str]:
    """Basnamnen som Dockerfile kopierar in i imagen."""
    joined = re.sub(r"\\[ \t]*\n", " ", dockerfile)
    files: set[str] = set()
    for line in joined.splitlines():
        tokens = line.strip().split()
        if not tokens or tokens[0].upper() not in ("COPY", "ADD"):
            continue
        # Flaggor som --chown/--from är inte källor; sista token är målet.
        sources = [token for token in tokens[1:] if not token.startswith("--")]
        files.update(Path(source).name for source in sources[:-1])
    return files


class DockerfileModulesTest(unittest.TestCase):
    def test_every_imported_local_module_is_copied_into_the_image(self):
        modules = imported_modules()
        self.assertTrue(modules, "hittade inga lokala moduler att kontrollera")
        copied = copied_files(DOCKERFILE.read_text(encoding="utf-8"))

        missing = sorted(f"{name}.py" for name in modules if f"{name}.py" not in copied)

        self.assertEqual([], missing,
                         f"Dockerfile COPY saknar {missing}; en image skulle krascha vid start")

    def test_the_agent_entrypoint_itself_is_copied(self):
        self.assertIn(ENTRYPOINT.name, copied_files(DOCKERFILE.read_text(encoding="utf-8")))

    def test_check_catches_a_module_removed_from_the_copy_line(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        copied = copied_files(dockerfile)
        target = next(name for name in sorted(imported_modules()) if f"{name}.py" in copied)

        self.assertNotIn(f"{target}.py", copied_files(dockerfile.replace(f" {target}.py", "")))

    def test_line_continuations_and_flags_are_understood(self):
        sample = "COPY --chown=agent:agent a.py \\\n     b.py \\\n     /opt/app/\nRUN true\n"
        self.assertEqual({"a.py", "b.py"}, copied_files(sample))

    def test_generated_build_version_is_not_required(self):
        # _build_version.py skrivs av Dockerfile respektive PyInstaller-specen
        # och finns aldrig i repot; den ska därför inte krävas i COPY-raden.
        self.assertNotIn("_build_version", imported_modules())


if __name__ == "__main__":
    unittest.main()
