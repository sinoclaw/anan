"""
L5 Causality — Mining Quality Advisor (Subagent)
=================================================
评估 PatternMiner/CausalReasoner 的挖掘质量，判断阈值是否需要调整。

设计原则：
- Handler: PatternMiner/CausalReasoner 管数据结构和统计计算
- Subagent: 给定挖掘结果分布，判断阈值该紧/松/不变

数据流：
  PatternMiner.mine_now() → 发布 L5.pattern.discovered
    → MiningQualityAdvisor 评估产出质量
      → 建议调整哪些阈值
        → SelfTuner 消费建议 → 发 TuningAction
          → PatternMiner 接收 set_thresholds()

阈值调整逻辑（硬编码的启发式规则）：
- 产出 pattern 过多（>10） → 建议提高 min_support 或 min_confidence
- 产出 pattern 过少（=0）且历史事件多 → 建议降低 min_lift 或 min_confidence
- 产出 pattern 质量参差（有些 lift 很低） → 建议提高 min_lift
- 稳定产出高质量 pattern → 保持现状

为什么用 subagent：
- "质量好"是模糊的 — subagent 能结合系统当前状态判断
- 阈值调整涉及 trade-off — 需要推理而不是固定规则
- 未来可以学: "当 L6.health < 0.5 时，宁可多产出 pattern 也不要漏掉"
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("anan.L5.mining_advisor")

# ---------------------------------------------------------------------------
# Mining Quality Decision
# ---------------------------------------------------------------------------

@dataclass
class MiningDecision:
    recommend_adjust: bool          # 是否建议调整阈值
    adjust_direction: str           # "tighten" | "loosen" | "keep"
    min_support: Optional[int] = None
    min_confidence: Optional[float] = None
    min_lift: Optional[float] = None
    reasoning: str = ""

    VALID_DIRECTIONS = frozenset(["tighten", "loosen", "keep"])

    def to_dict(self) -> dict:
        return {
            "recommend_adjust": self.recommend_adjust,
            "adjust_direction": self.adjust_direction,
            "min_support": self.min_support,
            "min_confidence": self.min_confidence,
            "min_lift": self.min_lift,
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# Fallback handler (rule-based)
# ---------------------------------------------------------------------------

def fallback_assess(
    patterns_found: int,
    total_events: int,
    current_thresholds: dict,
    recent_decisions: Optional[list] = None,
) -> MiningDecision:
    """Rule-based fallback when subagent is unavailable.

    Heuristics:
    - Too many patterns → tighten (raise thresholds)
    - Too few patterns + enough events → loosen (lower thresholds)
    - Zero patterns + very few events → keep (not enough data)
    - Stable quality → keep
    """
    recent = recent_decisions or []
    decisions = [d.get("adjust_direction") for d in recent[-3:]]

    # -------------------------------------------------------------------------
    # Fallback handler (rule-based)
    # -------------------------------------------------------------------------

    # Too many patterns — tighten
    if patterns_found > 10:
        direction = "tighten"
        reasoning = f"产出过多({patterns_found}个)，建议提高阈值过滤噪音"
        if current_thresholds.get("min_lift", 1.5) < 2.5:
            new_lift = min(current_thresholds.get("min_lift", 1.5) + 0.3, 3.0)
            new_conf = current_thresholds.get("min_confidence", 0.6)
        elif current_thresholds.get("min_confidence", 0.6) < 0.8:
            new_lift = current_thresholds.get("min_lift", 1.5)
            new_conf = min(current_thresholds.get("min_confidence", 0.6) + 0.1, 0.9)
        else:
            # Already tight — suggest support increase
            new_lift = current_thresholds.get("min_lift", 1.5)
            new_conf = current_thresholds.get("min_confidence", 0.6)
            new_support = current_thresholds.get("min_support", 2) + 1
            reasoning += f"（支持度提升到 {new_support}）"
    # Zero patterns but enough events to mine
    elif patterns_found == 0 and total_events >= 100:
        direction = "loosen"
        reasoning = f"无产出({total_events}个事件)，建议降低阈值"
        new_lift = max(current_thresholds.get("min_lift", 1.5) - 0.3, 1.1)
        new_conf = max(current_thresholds.get("min_confidence", 0.6) - 0.1, 0.3)
    # Few patterns with decent events
    elif patterns_found > 0 and patterns_found <= 3 and total_events >= 50:
        direction = "loosen"
        reasoning = f"产出偏少({patterns_found}个)，建议适当降低阈值"
        new_lift = max(current_thresholds.get("min_lift", 1.5) - 0.2, 1.1)
        new_conf = current_thresholds.get("min_confidence", 0.6)
    else:
        direction = "keep"
        reasoning = f"产出正常({patterns_found}个)，保持阈值不变"
        new_lift = current_thresholds.get("min_lift", 1.5)
        new_conf = current_thresholds.get("min_confidence", 0.6)

    return MiningDecision(
        recommend_adjust=(direction != "keep"),
        adjust_direction=direction,
        min_support=None,  # support rarely needs changing
        min_confidence=round(new_conf, 2) if direction in ("tighten", "loosen") else None,
        min_lift=round(new_lift, 2) if direction in ("tighten", "loosen") else None,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Subagent prompt
# ---------------------------------------------------------------------------

MINING_QUALITY_PROMPT = """你是 anan 的 L5 PatternMiner 挖掘质量顾问。判断当前阈值设置是否需要调整。

## 当前阈值设置
CURRENT_THRESHOLDS:
{min_thresholds}

## 本次挖掘结果
PATTERNS_FOUND: {patterns_found}
TOTAL_EVENTS_IN_WINDOW: {total_events}

## 最近调整历史
RECENT_ADJUSTMENTS:
{recent_history}

## 决策标准
1. 产出 >10 个 pattern → 可能噪音太多，建议 tighten（提高阈值）
2. 产出 0 个但事件数 ≥100 → 数据足够但没挖到，建议 loosen（降低阈值）
3. 产出 1-3 个且事件数 ≥50 → 可能漏掉了，建议轻微 loosen
4. 连续两次同方向调整 → 可能过头了，反方向微调
5. 产出 4-10 个且阈值已合理 → keep
6. 事件数 <50 → 数据太少，keep（不确信）

## 阈值调整建议
- tighten: 提高 min_lift（更严格）或 min_confidence（更高要求共现）
- loosen: 降低 min_lift（更宽松）或 min_confidence（更低要求）
- keep: 不建议调整

## 输出格式（严格 JSON）
{{
  "recommend_adjust": true|false,
  "adjust_direction": "tighten"|"loosen"|"keep",
  "min_support": null,
  "min_confidence": 0.0-1.0或null,
  "min_lift": 1.0-5.0或null,
  "reasoning": "判断理由（1-3句）"
}}"""


# ---------------------------------------------------------------------------
# Mining Quality Advisor
# ---------------------------------------------------------------------------

class MiningQualityAdvisor:
    """Subagent for evaluating pattern mining quality and recommending threshold adjustments.

    Usage:
        advisor = MiningQualityAdvisor(delegate_fn=delegate_task)
        decision = await advisor.assess(
            patterns_found=5,
            total_events=200,
            current_thresholds={"min_support": 2, "min_confidence": 0.6, "min_lift": 1.5},
            recent_decisions=[{"adjust_direction": "loosen", "min_lift": 1.2}],
        )
    """

    def __init__(
        self,
        delegate_fn: Optional[callable] = None,
        recent_decisions: Optional[list] = None,
    ):
        self._delegate_fn = delegate_fn
        self._recent_decisions: list[dict] = recent_decisions or []

    def set_delegate(self, fn: callable) -> None:
        self._delegate_fn = fn

    async def assess(
        self,
        patterns_found: int,
        total_events: int,
        current_thresholds: dict,
        recent_decisions: Optional[list] = None,
    ) -> MiningDecision:
        """Assess mining quality and recommend threshold adjustments."""
        recent = recent_decisions or self._recent_decisions

        # Build recent history text
        history_lines = []
        for d in recent[-3:]:
            dir_ = d.get("adjust_direction", "?")
            lift = d.get("min_lift", d.get("min_lift"))
            conf = d.get("min_confidence")
            history_lines.append(
                f"  {dir_}: lift={lift}, conf={conf}"
            )
        history_text = "\n".join(history_lines) or "  （无历史）"

        # Build thresholds text
        thresh_text = "\n".join(
            f"  {k}={v}" for k, v in current_thresholds.items()
        ) or "  （无）"

        prompt = MINING_QUALITY_PROMPT.format(
            min_thresholds=thresh_text,
            patterns_found=patterns_found,
            total_events=total_events,
            recent_history=history_text,
        )

        if not self._delegate_fn:
            logger.debug("MiningQualityAdvisor: no delegate_fn, using fallback")
            result = fallback_assess(patterns_found, total_events, current_thresholds, recent)
            self._recent_decisions.append(result.to_dict())
            return result

        try:
            result_text = await self._delegate_fn(
                goal="L5 阈值调整评估",
                context=prompt,
                parent_agent=None,
            )
            decision = self._parse_response(result_text)
            logger.info(
                "MiningQualityAdvisor: %d patterns from %d events → %s (adjust=%s)",
                patterns_found, total_events,
                decision.adjust_direction, decision.recommend_adjust,
            )
            self._recent_decisions.append(decision.to_dict())
            return decision
        except Exception as exc:
            logger.warning(
                "MiningQualityAdvisor subagent failed: %s, falling back", exc,
            )
            result = fallback_assess(patterns_found, total_events, current_thresholds, recent)
            self._recent_decisions.append(result.to_dict())
            return result

    @staticmethod
    def _parse_response(text: str) -> MiningDecision:
        """Parse subagent text response into MiningDecision."""
        # Strategy 1: ```json ... ```
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                return MiningQualityAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        # Strategy 2: raw {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return MiningQualityAdvisor._from_data(data)
            except json.JSONDecodeError:
                pass

        logger.warning("MiningQualityAdvisor: could not parse: %s", text[:200])
        return MiningDecision(
            recommend_adjust=False,
            adjust_direction="keep",
            reasoning="解析失败，保持阈值不变",
        )

    @staticmethod
    def _from_data(data: dict) -> MiningDecision:
        raw_direction = data.get("adjust_direction", "keep")
        if raw_direction not in MiningDecision.VALID_DIRECTIONS:
            raw_direction = "keep"

        return MiningDecision(
            recommend_adjust=bool(data.get("recommend_adjust", False)),
            adjust_direction=raw_direction,
            min_support=data.get("min_support"),
            min_confidence=data.get("min_confidence"),
            min_lift=data.get("min_lift"),
            reasoning=data.get("reasoning", ""),
        )
