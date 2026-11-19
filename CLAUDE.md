## Beta mode: move fast
- Don't write new tests unless the prompt asks for them.
- Only run tests for the app you changed (python manage.py test <app>), not the full suite.
- Always run makemigrations and migrate when models change.
- Keep changes focused; don't refactor unrelated code.
