"""ISO-to-engine translation, not organization-specific language selection."""

from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from .base import dependency


def language_codes(languages: Sequence[str]) -> dict[str, str]:
    if not languages:
        raise ValueError("Organization languages must not be empty")
    countries = dependency("pycountry", "parsing")
    codes: dict[str, str] = {}
    # Tesseract exceptions to ISO 639-3 nomenclature.
    aliases = {"zho": "chi_sim", "bod": "bod", "fil": "fil"}
    for language in languages:
        entry = countries.languages.get(alpha_2=language)
        if entry is None:
            raise ValueError(f"Unsupported language ISO code: {language!r}; expected ISO 639-1")
        codes[language] = aliases.get(entry.alpha_3, entry.alpha_3)
    return codes


def detected_language(text: str, languages: Sequence[str]) -> str:
    language_codes(languages)
    if len(languages) == 1 or not text.strip():
        return languages[0]
    code, _confidence = _identifier(tuple(languages)).classify(text)
    return str(code)


@lru_cache(maxsize=16)
def _identifier(languages: tuple[str, ...]) -> Any:
    module = dependency("langid.langid", "parsing")
    identifier = module.LanguageIdentifier.from_modelstring(module.model, norm_probs=True)
    try:
        identifier.set_languages(list(languages))
    except ValueError as error:
        raise ValueError(f"Unsupported language for local detection: {list(languages)}") from error
    return identifier
