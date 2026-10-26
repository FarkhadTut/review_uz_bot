"""Message catalogs: one flat key -> text YAML per language. Texts are HTML (parse_mode=HTML)
and use str.format placeholders; callers must html-escape user-supplied values."""

from functools import cache
from pathlib import Path

import yaml

LANGS = ("uz_cyrl", "uz_latn", "ru")
# The channel posts in Uzbek Cyrillic; channel_* keys exist only in this catalog.
CHANNEL_LANG = "uz_cyrl"

_DIR = Path(__file__).parent


@cache
def catalog(lang: str) -> dict[str, str]:
    with (_DIR / f"{lang}.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def t(lang: str | None, key: str, **kw: object) -> str:
    text = catalog(lang if lang in LANGS else CHANNEL_LANG)[key]
    return text.format(**kw) if kw else text
