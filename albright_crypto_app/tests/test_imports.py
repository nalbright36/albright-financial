"""No import cycles in albright_crypto_app: each module that pulls in the
collectors' helpers is imported FIRST in a fresh Python process (in this
process everything is already loaded, so a cycle would never show). The
shared helpers live in collectors/utils.py, which imports no collector, and
collectors/__init__.py builds COLLECTORS lazily."""
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

FIRST_IMPORTS = [
    "albright_crypto_app.templatetags.crypto_links",
    "albright_crypto_app.collectors.utils",
    "albright_crypto_app.collectors.json_api",
    "albright_crypto_app.collectors.job_boards",
    "albright_crypto_app.collectors",
    "albright_crypto_app.inbox_views",
    "albright_crypto_app.pipeline",
]
SCRIPT = """
import importlib, sys, django
django.setup()
importlib.import_module(sys.argv[1])
from albright_crypto_app.collectors import COLLECTORS
assert len(COLLECTORS) == 11, COLLECTORS
print(",".join(sorted(m.split(".")[-1] for m in sys.modules if m.startswith("albright_crypto_app.collectors."))))
"""


class ImportCycleTests(SimpleTestCase):
    def run_first(self, module):
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE",
                                                                       "albright_trading_proj.settings")}
        return subprocess.run([sys.executable, "-c", SCRIPT, module], cwd=Path(settings.BASE_DIR), env=env,
                              capture_output=True, text=True, timeout=120)

    def test_every_entry_point_imports_first_without_a_cycle(self):
        for module in FIRST_IMPORTS:
            with self.subTest(module=module):
                result = self.run_first(module)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertNotIn("partially initialized", result.stderr)

    def test_helpers_module_imports_nothing_from_the_app(self):
        source = (Path(settings.BASE_DIR) / "albright_crypto_app" / "collectors" / "utils.py").read_text(encoding="utf-8")
        imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        self.assertEqual(imports, ["import html", "import re", "from urllib.parse import urlparse"])
