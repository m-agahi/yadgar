"""Entity extraction patterns and functions for query analysis."""

import re

from yadgar.observability.observe import observe

# Lightweight entity extraction for queries (subset of consolidation patterns)
_WORD_RE = re.compile(r"\b[A-Z][\w]*(?:[A-Z][\w]*)*\b")  # CamelCase
_PATH_RE = re.compile(r"(?:\.{0,2}/)?(?:[\w@.-]+/)+[\w@.-]+\.\w+")
_DOTTED_RE = re.compile(r"\b\w+(?:\.\w+){1,}\b")  # dotted.names
_ERROR_RE = re.compile(r"\b\w*(?:Error|Exception)\b")
_KEYWORD_RE = re.compile(r"\b[a-z_]\w{2,}\b")

_QUESTION_WORDS = {"who", "what", "where", "when", "why", "how", "which", "whose", "whom"}
_QUERY_STOP_WORDS = _QUESTION_WORDS | {
    "would",
    "could",
    "should",
    "does",
    "did",
    "is",
    "are",
    "was",
    "were",
    "do",
    "has",
    "have",
    "had",
    "can",
    "will",
    "the",
    "a",
    "an",
    "if",
    "not",
    "it",
    "in",
    "on",
    "at",
    "to",
    "of",
    "for",
    "and",
    "or",
    "but",
    "with",
    "by",
    "from",
    "as",
    "be",
    "this",
    "that",
    "so",
    "no",
    "yes",
    "my",
    "your",
    "its",
    "our",
    "their",
    "we",
    "he",
    "she",
    "they",
    "me",
    "him",
    "her",
    "us",
    "them",
    "about",
    "also",
    "likely",
    "probably",
    "possibly",
    "still",
    "more",
    "most",
    "very",
    "around",
    "what's",
    "whats",
    "interested",
    "going",
    "considered",
    "consider",
    "considering",
}
_OPEN_DOMAIN_MODAL_WORDS = {"would", "could", "might", "likely", "probably", "possibly"}
_OPEN_DOMAIN_CUE_PHRASES = (
    "personality trait",
    "personality traits",
    "what attributes",
    "political leaning",
    "financial status",
    "religious",
    "patriotic",
    "career",
    "job might",
    "degree",
    "interested in",
    "more interested",
    "enjoy the song",
    "bookshelf",
    "member of the lgbtq",
    "ally",
)
_OPEN_DOMAIN_TOPIC_EXPANSIONS = {
    "national park": ["camping", "outdoors", "nature", "hiking"],
    "theme park": ["rides", "amusement park"],
    "dr. seuss": ["children's books", "kids books", "classic books"],
    "bookshelf": ["books", "reading", "collects"],
    "vivaldi": ["classical music", "orchestra", "composer"],
    "four seasons": ["classical music", "orchestra", "composer"],
    "patriotic": ["serve country", "military", "public service"],
    "religious": ["faith", "church", "spiritual"],
    "political leaning": ["lgbtq rights", "liberal", "progressive"],
    "financial status": ["money", "wealth", "afford", "comfortable"],
    "personality traits": ["thoughtful", "authentic", "driven", "caring"],
    "attributes": ["thoughtful", "authentic", "driven", "caring"],
    "ally": ["supportive", "accepting", "rights"],
    "member of the lgbtq": ["lgbtq", "transgender", "queer"],
}
_OPEN_DOMAIN_FACT_PATTERNS = (
    (
        re.compile(
            r"^(?P<subject>[A-Z][a-z]+)\s+(?:really\s+|also\s+|just\s+|always\s+|"
            r"often\s+|never\s+|still\s+|definitely\s+|usually\s+|sometimes\s+)?"
            r"(?:loves|likes|enjoys|prefers|adores)\s+(?P<object>[^.!?\n]+)",
            re.IGNORECASE,
        ),
        "{subject} enjoys {object}",
    ),
    (
        re.compile(
            r"^(?P<subject>[A-Z][a-z]+)\s+is\s+(?:a fan of|into|keen on|"
            r"interested in|passionate about)\s+(?P<object>[^.!?\n]+)",
            re.IGNORECASE,
        ),
        "{subject} is interested in {object}",
    ),
    (
        re.compile(
            r"^(?P<subject>[A-Z][a-z]+)\s+(?:wants|hopes|plans|dreams)\s+to\s+"
            r"(?P<object>[^.!?\n]+)",
            re.IGNORECASE,
        ),
        "{subject} wants to {object}",
    ),
)
_SAID_LINE_RE = re.compile(r"^(?P<speaker>[A-Z][a-z]+) said:\s*(?P<quote>.+)$")
_LINE_SUBJECT_RE = re.compile(r"^(?P<subject>[A-Z][a-z]+)\b")


@observe(tier="hot", name="retrieval.entities.extract_query_entities")
def _extract_query_entities(query: str) -> list[str]:
    """Extract key concepts/entities from a query string."""
    entities: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen and len(name) > 1:
            seen.add(name)
            entities.append(name)

    for m in _PATH_RE.finditer(query):
        _add(m.group(0))
    for m in _DOTTED_RE.finditer(query):
        _add(m.group(0))
    for m in _ERROR_RE.finditer(query):
        _add(m.group(0))
    for m in _WORD_RE.finditer(query):
        _add(m.group(0))
    # Also split on whitespace for simple keyword matching
    for word in query.split():
        cleaned = word.strip(".,;:!?()[]{}\"'")
        if len(cleaned) > 2:
            _add(cleaned)

    return entities
