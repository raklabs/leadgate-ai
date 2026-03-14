"""Configurable, deterministic rule-based lead scoring engine (Spec 001 Section 4)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import ExtractedEnquiryData, LeadTier, ScoreBreakdown, ScoreComponent


class BudgetBracketRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_amount: float
    points: int
    label: str


class TierThresholdsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hot: int = 75
    warm: int = 50
    medium: int = 25


class ContactCompletenessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    both_email_and_phone: int = 10
    either_email_or_phone: int = 5
    neither: int = 0


class ScoringRulesConfig(BaseModel):
    """Validated schema for config/scoring_rules.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    min_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    tier_thresholds: TierThresholdsConfig = Field(default_factory=TierThresholdsConfig)
    service_weights: dict[str, int]
    budget_brackets: list[BudgetBracketRule]
    budget_missing_points: int = 5
    timeline_weights: dict[str, int]
    urgency_weights: dict[str, int]
    contact_completeness: ContactCompletenessConfig = Field(default_factory=ContactCompletenessConfig)


class RuleBasedLeadScorer:
    """Evaluates extracted enquiry fields against configurable YAML business rules."""

    def __init__(self, rules_config: ScoringRulesConfig) -> None:
        self.rules = rules_config

    @classmethod
    def from_yaml(cls, path: str | Path) -> RuleBasedLeadScorer:
        """Load and validate scoring rules from a YAML file."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        config = ScoringRulesConfig.model_validate(raw)
        return cls(config)

    def score(self, data: ExtractedEnquiryData) -> ScoreBreakdown:
        """Compute deterministic score (0-100), tier (Hot/Warm/Medium/Cold), and audit trail."""
        if data.is_spam_or_irrelevant:
            return ScoreBreakdown(
                total_score=0,
                tier=LeadTier.COLD,
                rule_version=self.rules.version,
                components=[
                    ScoreComponent(
                        dimension="spam_filter",
                        points=0,
                        reason="Flagged as spam or non-sales enquiry -> forced to 0 (Cold)",
                    )
                ],
            )

        components: list[ScoreComponent] = []

        # 1. Service / Property Type
        service_key = data.service_or_property_type.value if data.service_or_property_type else "missing"
        service_pts = self.rules.service_weights.get(service_key, 0)
        components.append(
            ScoreComponent(
                dimension="service_or_property_type",
                points=service_pts,
                reason=f"Service category '{service_key}' (+{service_pts} pts)",
            )
        )

        # 2. Budget (use budget_max if present, else budget_min)
        effective_budget = data.budget_max if data.budget_max is not None else data.budget_min
        if effective_budget is None:
            budget_pts = self.rules.budget_missing_points
            components.append(
                ScoreComponent(
                    dimension="budget",
                    points=budget_pts,
                    reason=f"Budget not explicitly stated (+{budget_pts} neutral baseline pts)",
                )
            )
        else:
            sorted_brackets = sorted(self.rules.budget_brackets, key=lambda b: b.min_amount, reverse=True)
            matched_bracket = next(
                (b for b in sorted_brackets if effective_budget >= b.min_amount),
                BudgetBracketRule(min_amount=0, points=0, label="Below minimum"),
            )
            components.append(
                ScoreComponent(
                    dimension="budget",
                    points=matched_bracket.points,
                    reason=f"{matched_bracket.label}: {effective_budget:,.0f} (+{matched_bracket.points} pts)",
                )
            )

        # 3. Timeline
        timeline_key = data.timeline.value if data.timeline else "missing"
        timeline_pts = self.rules.timeline_weights.get(timeline_key, 0)
        components.append(
            ScoreComponent(
                dimension="timeline",
                points=timeline_pts,
                reason=f"Timeline '{timeline_key}' (+{timeline_pts} pts)",
            )
        )

        # 4. Urgency
        urgency_key = data.urgency.value if data.urgency else "missing"
        urgency_pts = self.rules.urgency_weights.get(urgency_key, 0)
        components.append(
            ScoreComponent(
                dimension="urgency",
                points=urgency_pts,
                reason=f"Urgency '{urgency_key}' (+{urgency_pts} pts)",
            )
        )

        # 5. Contact Completeness
        has_email = bool(data.contact_email and data.contact_email.strip())
        has_phone = bool(data.contact_phone and data.contact_phone.strip())
        if has_email and has_phone:
            contact_pts = self.rules.contact_completeness.both_email_and_phone
            contact_reason = f"Both email and phone provided (+{contact_pts} pts)"
        elif has_email or has_phone:
            contact_pts = self.rules.contact_completeness.either_email_or_phone
            contact_reason = f"Single contact method provided (+{contact_pts} pts)"
        else:
            contact_pts = self.rules.contact_completeness.neither
            contact_reason = "No contact email or phone provided (+0 pts)"
        components.append(
            ScoreComponent(
                dimension="contact_completeness",
                points=contact_pts,
                reason=contact_reason,
            )
        )

        raw_total = sum(c.points for c in components)
        total_score = max(0, min(100, raw_total))
        tier = self._resolve_tier(total_score)

        return ScoreBreakdown(
            total_score=total_score,
            tier=tier,
            rule_version=self.rules.version,
            components=components,
        )

    def _resolve_tier(self, score: int) -> LeadTier:
        if score >= self.rules.tier_thresholds.hot:
            return LeadTier.HOT
        if score >= self.rules.tier_thresholds.warm:
            return LeadTier.WARM
        if score >= self.rules.tier_thresholds.medium:
            return LeadTier.MEDIUM
        return LeadTier.COLD

