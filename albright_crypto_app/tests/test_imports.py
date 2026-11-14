"""Core Opportunities modules still import in a fresh process (no cycles)."""
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

FIRST_IMPORTS = [
    "albright_crypto_app.templatetags.crypto_links",
    "albright_crypto_app.inbox_views",
    "albright_crypto_app.pipeline",
    "albright_labs.guards",
    "albright_labs.links",
    "albright_labs.ai_tasks",
]
SCRIPT = """
import importlib, sys, django
django.setup()
importlib.import_module(sys.argv[1])
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
