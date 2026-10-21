from app.config import settings
from app.inference.providers.chain import ModelChain


def _fallbacks() -> list[str]:
    return [model.strip() for model in (settings.groq_fallback_models or "").split(",") if model.strip()]


class InferenceRouter:
    """Model roles.

    Chat turns use one model, the strongest (``deep``): when it is busy the turn waits
    for it, it is never handed to a weaker model (owner, 2026-10-16). Background helpers
    (titles, summaries, review analysis, tagging) use the small model's chain, which
    leaves the chat model's rate limit to chat. Web search is Gemini's (grounded search).
    """

    def __init__(self):
        self._fast = None
        self._deep = None
        self._vision = None

    def fast(self):
        if self._fast is None:
            self._fast = ModelChain([settings.groq_model, *_fallbacks()])
        return self._fast

    def deep(self):
        if self._deep is None:
            self._deep = ModelChain([settings.groq_deep_model])
        return self._deep

    def summary(self):
        """Background summaries: the small model first, so chat turns keep their headroom."""
        if getattr(self, "_summary", None) is None:
            self._summary = ModelChain([settings.groq_summary_model, *_fallbacks(), settings.groq_model])
        return self._summary

    def vision(self):
        if self._vision is None:
            self._vision = ModelChain([settings.groq_vision_model])
        return self._vision

    def for_policy(self, policy):
        depth = getattr(policy, "depth", None) or getattr(policy, "reasoning_level", None)
        return self.deep() if depth in {"deep", "research"} else self.fast()
