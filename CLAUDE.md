## Beta mode: move fast
- Don't write new tests unless the prompt asks for them.
- Only run tests for the app you changed (python manage.py test <app>), not the full suite.
- Always run makemigrations and migrate when models change.
- Keep changes focused; don't refactor unrelated code.

## Home tiles: register new pages and actions
- Any new page someone would open from Home, and any new "create / log" action, gets a tile in
  `albright_labs/home_tiles.py` (`PAGES`) in the same change: a page as `Tile(...)`, an action as `_act(...)`.
- An action that fits in a few fields also gets a quick-add popup: an entry in `albright_labs/quick_actions.py`, a
  template in `albright_labs/templates/albright_labs/quick/`, and a JSON reply in the app's own save view
  (`albright_labs/quick.py`: `if quick.wanted(request): return quick.ok(...)`). Reuse the app's form and save code.
