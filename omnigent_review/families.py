"""Model-family resolution for dispatch independence checks.

Family belongs to the model, not the harness: opencode, hermes, cursor and pi
can run any vendor's model, so only single-family harnesses are inferred from
the agent name and everything else needs an explicit, recognized model.
"""

from __future__ import annotations

from collections.abc import Mapping

SINGLE_FAMILY_AGENTS: Mapping[str, str] = {
    "claude_code": "anthropic",
    "codex": "openai",
    "agy": "google",
}

MODEL_PREFIX_FAMILIES: Mapping[str, str] = {
    "claude": "anthropic",
    "opus": "anthropic",
    "sonnet": "anthropic",
    "haiku": "anthropic",
    "fable": "anthropic",
    "gpt": "openai",
    "codex": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "gemini": "google",
    "deepseek": "deepseek",
    "kimi": "moonshot",
    "glm": "zhipu",
    "qwen": "alibaba",
    "grok": "xai",
}


def family_of_model(
    model: str,
    prefixes: Mapping[str, str] = MODEL_PREFIX_FAMILIES,
) -> str | None:
    """
    Map a model id to its family by prefix.

    :param model: Model id, optionally provider-qualified, e.g.
        ``"openai/gpt-5.1"`` or ``"claude-opus-4-8"``.
    :param prefixes: Lowercase model-name prefix to family.
    :returns: The family, or ``None`` when the model is not recognized.
    """
    name = model.strip().lower().rsplit("/", 1)[-1]
    for prefix, family in prefixes.items():
        if name.startswith(prefix):
            return family
    return None


def resolve_family(
    agent: str,
    model: str | None,
    prefixes: Mapping[str, str] = MODEL_PREFIX_FAMILIES,
) -> str | None:
    """
    Resolve the family a dispatch will actually run, failing closed.

    :param agent: Worker agent name, e.g. ``"codex"`` or ``"opencode"``.
    :param model: Explicit ``args.model`` for the dispatch, or ``None``.
    :param prefixes: Lowercase model-name prefix to family.
    :returns: The family, or ``None`` when it cannot be proven: an unrecognized
        model, a multi-model harness with no explicit model, or an explicit
        model that contradicts a single-family harness.
    """
    harness_family = SINGLE_FAMILY_AGENTS.get(agent)
    if not model:
        return harness_family
    model_family = family_of_model(model, prefixes)
    if harness_family is not None and model_family != harness_family:
        return None
    return model_family
