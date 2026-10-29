"""Test helpers for the header's app launcher (templates/index.html), shared by the apps' tests."""
import re

TILE = re.compile(r'<a href="([^"]*)" class="(app-launcher__tile[^"]*)" role="menuitem" tabindex="-1" '
                  r'data-app-key="(\w+)" data-app-target="(\w+)".*?<span class="app-launcher__label">([^<]+)</span>', re.S)


def launcher_panel(content):
    """The launcher panel's tiles markup, or "" when the header has no launcher."""
    if 'id="app-launcher-panel"' not in content:
        return ""
    return content.split('id="app-launcher-panel"', 1)[1].split('class="app-launcher__customize"', 1)[0]


def launcher_tiles(content):
    """[(data-app-target, label), ...] of the launcher's tiles, in order."""
    return [(target, label) for _href, _cls, _key, target, label in TILE.findall(launcher_panel(content))]


def launcher_links(content):
    """{label: href} of the launcher's tiles."""
    return {label: href for href, _cls, _key, _target, label in TILE.findall(launcher_panel(content))}


def current_tiles(content):
    """data-app-target of the highlighted (current app) tile(s)."""
    return [target for _href, cls, _key, target, _label in TILE.findall(launcher_panel(content)) if "is-current" in cls]
