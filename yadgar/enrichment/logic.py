"""Rule-based natural logic expansion. No external models."""

import re

_HYPERNYM_MAP = {
    # Places
    "yellowstone": "national_park",
    "yosemite": "national_park",
    "grand canyon": "national_park",
    "zion": "national_park",
    "glacier": "national_park",
    "everglades": "national_park",
    "sequoia": "national_park",
    "acadia": "national_park",
    "denali": "national_park",
    "rocky mountain": "national_park",
    "paris": "city",
    "london": "city",
    "tokyo": "city",
    "new york": "city",
    # People / cultural
    "bach": "classical_music",
    "beethoven": "classical_music",
    "mozart": "classical_music",
    "chopin": "classical_music",
    "picasso": "visual_art",
    "monet": "visual_art",
    "van gogh": "visual_art",
    "shakespeare": "literature",
    "hemingway": "literature",
    "tolkien": "literature",
    # Languages
    "python": "programming_language",
    "javascript": "programming_language",
    "rust": "programming_language",
    "java": "programming_language",
    "typescript": "programming_language",
    # Animals
    "labrador": "dog",
    "golden retriever": "dog",
    "siamese": "cat",
    "persian": "cat",
}

_VERB_NOMINALIZATIONS = {
    "camping": "camping trip",
    "hiking": "hiking trip",
    "fishing": "fishing trip",
    "traveling": "travel journey",
    "travelling": "travel journey",
    "reading": "reading hobby",
    "cooking": "cooking activity",
    "painting": "painting activity",
    "gardening": "gardening hobby",
    "swimming": "swimming activity",
    "running": "running exercise",
    "cycling": "cycling activity",
    "dancing": "dancing activity",
    "writing": "writing activity",
    "singing": "singing performance",
    "climbing": "climbing adventure",
    "surfing": "surfing activity",
    "skiing": "skiing trip",
    "snowboarding": "snowboarding trip",
    "volunteering": "volunteer work",
    "meditating": "meditation practice",
    "studying": "study session",
    "practicing": "practice session",
    "teaching": "teaching session",
    "learning": "learning experience",
    "exploring": "exploration adventure",
}

# Patterns: ("verb/trigger word", "nominalization")
_VERB_PATTERN = re.compile(
    r"\b(?:went|goes|go|enjoys?|loves?|likes?|started|began|tries?|tried)\s+"
    r"(\w+ing)\b",
    re.IGNORECASE,
)


class LogicExpander:
    """Rule-based natural logic expansion. No external models."""

    def expand(self, content: str) -> list[str]:
        expansions: list[str] = []
        content_lower = content.lower()

        # Hypernym lifting
        for term, hypernym in _HYPERNYM_MAP.items():
            if term in content_lower:
                if hypernym not in expansions:
                    expansions.append(hypernym)

        # Verb nominalization: "went camping" → "camping trip"
        for match in _VERB_PATTERN.finditer(content):
            gerund = match.group(1).lower()
            nominalization = _VERB_NOMINALIZATIONS.get(gerund)
            if nominalization and nominalization not in expansions:
                expansions.append(nominalization)

        # Also check for standalone gerunds in content
        tokens = set(re.findall(r"\b\w+ing\b", content_lower))
        for gerund in tokens:
            nominalization = _VERB_NOMINALIZATIONS.get(gerund)
            if nominalization and nominalization not in expansions:
                expansions.append(nominalization)

        return expansions
