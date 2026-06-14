"""ConceptNet-based concept expansion for enrichment."""

import re

from yadgar.config import Settings

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
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
        "not",
        "with",
        "by",
        "from",
        "as",
        "be",
        "was",
        "were",
        "been",
        "are",
        "am",
        "do",
        "did",
        "does",
        "has",
        "had",
        "have",
        "will",
        "would",
        "could",
        "should",
        "may",
        "can",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "if",
        "then",
        "so",
        "no",
        "yes",
        "all",
        "any",
        "some",
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
        "i",
        "use",
        "using",
        "used",
        "like",
        "just",
        "get",
        "got",
        "set",
        "make",
        "made",
        "let",
        "try",
        "need",
        "want",
        "know",
        "think",
        "really",
        "very",
        "also",
        "about",
        "into",
        "over",
        "such",
        "being",
        "going",
        "went",
        "come",
        "came",
        "said",
    }
)

HARDCODED_EXPANSIONS = {
    "camping": ["outdoor_activity", "nature", "tent", "hiking", "national_park", "wilderness"],
    "hiking": ["outdoor_activity", "trail", "nature", "exercise", "mountain", "national_park"],
    "fishing": ["outdoor_activity", "water", "lake", "river", "nature", "patience"],
    "painting": ["art", "creative", "visual_art", "canvas", "artistic", "hobby"],
    "reading": ["book", "literature", "knowledge", "education", "hobby", "library"],
    "cooking": ["food", "kitchen", "recipe", "culinary", "meal", "hobby"],
    "gardening": ["plant", "nature", "outdoor_activity", "hobby", "flower", "garden"],
    "yoga": ["exercise", "meditation", "fitness", "health", "relaxation", "flexibility"],
    "running": ["exercise", "fitness", "marathon", "jogging", "sport", "cardio"],
    "swimming": ["water", "exercise", "pool", "sport", "fitness", "aquatic"],
    "piano": ["music", "instrument", "classical", "keyboard", "musical", "performance"],
    "violin": ["music", "instrument", "classical", "string", "orchestra", "musical"],
    "guitar": ["music", "instrument", "string", "acoustic", "musical", "performance"],
    "photography": ["camera", "art", "visual", "creative", "hobby", "image"],
    "travel": ["adventure", "tourism", "explore", "vacation", "journey", "destination"],
    "volunteering": ["charity", "community", "altruism", "helping", "service", "kindness"],
    "meditation": ["mindfulness", "relaxation", "mental_health", "calm", "peace", "spiritual"],
    "cycling": ["bicycle", "exercise", "sport", "fitness", "outdoor_activity", "commute"],
    "dancing": ["music", "art", "performance", "exercise", "rhythm", "movement"],
    "writing": ["literature", "creative", "author", "storytelling", "hobby", "expression"],
}


def _extract_terms(content: str) -> list[str]:
    """Extract content-bearing nouns/verbs via simple tokenization."""
    tokens = re.findall(r"[a-zA-Z]+", content.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 2]


class ConceptNetExpander:
    """Query ConceptNet for related concepts."""

    def __init__(self) -> None:
        self._conceptnet_lite = None
        self._lite_available: bool | None = None
        self._http_available: bool | None = None

    def _try_lite(self, term: str, relations: list[str], min_weight: float) -> list[str]:
        """Try conceptnet_lite local SQLite database."""
        if self._lite_available is False:
            return []
        try:
            if self._conceptnet_lite is None:
                import conceptnet_lite

                self._conceptnet_lite = conceptnet_lite
            self._lite_available = True

            results = []
            for rel in relations:
                edges = self._conceptnet_lite.query(node=f"/c/en/{term}", rel=f"/r/{rel}", limit=10)
                for edge in edges:
                    if edge.weight >= min_weight:
                        # Extract the end node label
                        end = edge.end.label if hasattr(edge.end, "label") else str(edge.end)
                        results.append(end.replace(" ", "_"))
            return results
        except (ImportError, Exception) as _e:
            self._lite_available = False
            return []

    @staticmethod
    def _parse_edges(data: dict, min_weight: float) -> list[str]:
        """Extract concept labels from a ConceptNet API response, filtered by weight."""
        results = []
        for edge in data.get("edges", []):
            if edge.get("weight", 0) >= min_weight:
                end = edge.get("end", {}).get("label", "")
                if end:
                    results.append(end.replace(" ", "_"))
        return results

    def _try_http(self, term: str, relations: list[str], min_weight: float) -> list[str]:
        """Try ConceptNet HTTP API. Disabled by default — too slow for batch use."""
        # HTTP API is 5s/request — unusable for batch enrichment
        # Skip straight to hardcoded. Enable via self._http_available = None to test.
        if self._http_available is not True:
            return []
        try:
            import urllib.parse

            import httpx

            results = []
            # §8: URL-encode term to prevent injection; use HTTPS; httpx only.
            safe_term = urllib.parse.quote(term, safe="")
            for rel in relations:
                url = (
                    f"https://api.conceptnet.io/query?node=/c/en/{safe_term}&rel=/r/{rel}&limit=10"
                )
                try:
                    resp = httpx.get(url, headers={"Accept": "application/json"}, timeout=5.0)
                    resp.raise_for_status()
                    results.extend(self._parse_edges(resp.json(), min_weight))
                except (httpx.RequestError, httpx.HTTPStatusError, TimeoutError, OSError) as _e:
                    continue
            self._http_available = True if results else None
            return results
        except Exception:
            self._http_available = False
            return []

    def _try_hardcoded(self, term: str) -> list[str]:
        """Fall back to hardcoded expansions."""
        return list(HARDCODED_EXPANSIONS.get(term, []))

    def expand(self, content: str, settings: Settings) -> list[str]:
        relations = [r.strip() for r in settings.CONCEPTNET_RELATIONS.split(",")]
        min_weight = settings.CONCEPTNET_MIN_EDGE_WEIGHT
        max_terms = settings.CONCEPTNET_MAX_TERMS

        terms = _extract_terms(content)
        all_concepts: list[str] = []
        seen: set[str] = set()

        for term in terms:
            # Try sources in order: lite → HTTP → hardcoded
            concepts = self._try_lite(term, relations, min_weight)
            if not concepts:
                concepts = self._try_http(term, relations, min_weight)
            if not concepts:
                concepts = self._try_hardcoded(term)

            for c in concepts:
                if c not in seen:
                    seen.add(c)
                    all_concepts.append(c)

            if len(all_concepts) >= max_terms:
                break

        return all_concepts[:max_terms]
