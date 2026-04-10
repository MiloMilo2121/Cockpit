from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.config import settings


class ModelTier(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


@dataclass(frozen=True)
class ModelRoute:
    requested_tier: ModelTier
    effective_tier: ModelTier
    models: tuple[str, ...]
    reasoning: dict[str, Any] | None
    max_tokens: int
    paid_models_allowed: bool
    downgrade_reason: str = ""

    @property
    def primary_model(self) -> str:
        return self.models[0] if self.models else ""

    @property
    def tier_label(self) -> str:
        if self.requested_tier == self.effective_tier:
            return self.effective_tier.value
        return f"{self.requested_tier.value}->{self.effective_tier.value}"


HARD_SIGNALS = (
    "anomalia",
    "bloccante",
    "compliance",
    "contratto",
    "correzione",
    "data loss",
    "dead_letter",
    "deadline",
    "deploy",
    "errore critico",
    "gdpr",
    "incident",
    "legale",
    "migrazione",
    "privacy",
    "produzione",
    "rialloca",
    "risk",
    "root cause",
    "scostament",
    "security",
)

MEDIUM_SIGNALS = (
    "agenda",
    "analizza",
    "briefing",
    "calendar",
    "decisione",
    "email",
    "piano",
    "priorita",
    "priorità",
    "qdrant",
    "rag",
    "riassumi",
    "roadmap",
    "strategia",
    "task",
)

EASY_SIGNALS = (
    "classifica",
    "dedup",
    "estrai",
    "rerank",
    "sintesi breve",
    "tagga",
    "urgente/non urgente",
)


def infer_model_tier(*, instruction: str, priority: str | None = None, is_proactive: bool = False) -> ModelTier:
    text = instruction.lower()
    normalized_priority = str(priority or "").strip().lower()
    score = 0

    if normalized_priority in {"critical", "critica", "urgent", "urgente"}:
        score += 3
    elif normalized_priority in {"high", "alta"}:
        score += 2
    elif normalized_priority in {"medium", "media"}:
        score += 1

    if is_proactive:
        score += 1

    length = len(instruction)
    if length > 8000:
        score += 2
    elif length > 2000:
        score += 1

    if any(signal in text for signal in HARD_SIGNALS):
        score += 2
    if any(signal in text for signal in MEDIUM_SIGNALS):
        score += 1
    if any(signal in text for signal in EASY_SIGNALS):
        score -= 1

    if score >= 4:
        return ModelTier.HARD
    if score >= 2:
        return ModelTier.MEDIUM
    return ModelTier.EASY


def _models_for_tier(tier: ModelTier) -> list[str]:
    if tier == ModelTier.HARD:
        return settings.openrouter_hard_model_list
    if tier == ModelTier.MEDIUM:
        return settings.openrouter_medium_model_list
    return settings.openrouter_easy_model_list


def _allowed_models(models: list[str], *, tier: ModelTier) -> list[str]:
    if tier == ModelTier.EASY:
        return [model for model in models if model.endswith(":free")]
    if settings.openrouter_allow_paid_models:
        return models
    return [model for model in models if model.endswith(":free")]


def _reasoning_for_tier(tier: ModelTier) -> dict[str, Any] | None:
    if tier == ModelTier.HARD:
        return {"effort": "high", "exclude": True}
    if tier == ModelTier.MEDIUM:
        return {"effort": "medium", "exclude": True}
    return None


def select_model_route(
    *,
    instruction: str,
    priority: str | None = None,
    is_proactive: bool = False,
    requested_tier: ModelTier | str | None = None,
) -> ModelRoute:
    if isinstance(requested_tier, ModelTier):
        tier = requested_tier
    elif requested_tier:
        tier = ModelTier(str(requested_tier).strip().lower())
    else:
        tier = infer_model_tier(instruction=instruction, priority=priority, is_proactive=is_proactive)

    models = _allowed_models(_models_for_tier(tier), tier=tier)
    effective_tier = tier
    downgrade_reason = ""

    if not models:
        models = _allowed_models(settings.openrouter_easy_model_list, tier=ModelTier.EASY) or settings.openrouter_models
        effective_tier = ModelTier.EASY
        downgrade_reason = "paid_models_disabled"

    max_tokens = int(settings.openrouter_max_tokens)
    if effective_tier == ModelTier.MEDIUM:
        max_tokens = max(max_tokens, 900)
    elif effective_tier == ModelTier.HARD:
        max_tokens = max(max_tokens, 1200)

    return ModelRoute(
        requested_tier=tier,
        effective_tier=effective_tier,
        models=tuple(models),
        reasoning=_reasoning_for_tier(effective_tier),
        max_tokens=max_tokens,
        paid_models_allowed=bool(settings.openrouter_allow_paid_models),
        downgrade_reason=downgrade_reason,
    )
