"""
Sinoclaw Insight Sync — anan → sinoclaw 洞察同步
=================================================

把 anan 最新的 L9 wisdom_facts 和 L5 预测 accuracy 写入 sinoclaw 的 state.db，
作为 system 角色消息注入到活动 session，让 sinoclaw AIAgent 在下一轮对话时
能感知 anan 的认知状态。

工作流程:
  cron 触发 → 读取 ~/.anan/wisdom_latest.json
          → 写入 sinoclaw state.db sessions 表（role=system）
          → sinoclaw AIAgent 下次 chat() 时自动带入上下文

跑法（手动）:
    python3 -m adapters.sinoclaw_insight_sync

配置 cron（每小时一次）:
    sinoclaw cron create \
      --name "anan_insight_sync" \
      --prompt "python3 -m adapters.sinoclaw_insight_sync" \
      --schedule "0 * * * *"
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

# anan 的wisdom输出文件（由 live_causal_demo 或 anan 运行时写入）
WISDOM_FILE = Path.home() / ".anan" / "wisdom_latest.json"
# sinoclaw 的 session DB
SINOCLAW_DB = Path.home() / ".sinoclaw" / "state.db"


def load_latest_wisdom() -> dict | None:
    """读取 anan 最新的 wisdom 快照。"""
    if not WISDOM_FILE.exists():
        return None
    try:
        return json.loads(WISDOM_FILE.read_text())
    except Exception:
        return None


def write_to_sinoclaw_session(insight: dict) -> int | None:
    """把洞察写入 sinoclaw 最新活动的 session（role=system）。"""
    if not SINOCLAW_DB.exists():
        print(f"⚠️  sinoclaw state.db 不存在: {SINOCLAW_DB}")
        return None

    try:
        conn = sqlite3.connect(SINOCLAW_DB)
        cur = conn.cursor()

        # 找到最新的活动 session（按 updated_at 排序）
        cur.execute("""
            SELECT id FROM sessions
            ORDER BY started_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            print("⚠️  没有找到活动 session")
            conn.close()
            return None

        session_id = row[0]

        # 构建消息内容
        wisdom_items = insight.get("wisdom_facts", [])
        prediction_stats = insight.get("prediction_stats", {})
        causal_links = insight.get("causal_links", [])

        lines = ["【anan 认知状态报告】", f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

        if wisdom_items:
            lines.append(f"🔮 anan 当前洞察 ({len(wisdom_items)} 条):")
            for w in wisdom_items[-5:]:  # 最多5条
                lines.append(f"  • {w}")
            lines.append("")

        if causal_links:
            lines.append(f"⚡ 因果链路发现 ({len(causal_links)} 条):")
            for link in causal_links[:3]:
                lines.append(f"  • {link}")
            lines.append("")

        if prediction_stats:
            acc = prediction_stats.get("accuracy", 0)
            confirmed = prediction_stats.get("confirmed", 0)
            failed = prediction_stats.get("failed", 0)
            lines.append(
                f"🎯 预测准确率: {acc:.0%} "
                f"(确认 {confirmed}, 失败 {failed})"
            )

        content = "\n".join(lines)

        # 写入消息
        cur.execute("""
            INSERT INTO messages (session_id, role, content, timestamp)
            VALUES (?, 'system', ?, ?)
        """, (session_id, content, time.time()))

        # 更新时间戳（保持 session 在列表顶部）
        cur.execute("""
            UPDATE sessions SET ended_at = NULL WHERE id = ?
        """, (session_id,))

        conn.commit()
        conn.close()

        print(f"✅ 洞察已写入 sinoclaw session {session_id}")
        print(f"   wisdom={len(wisdom_items)} 条, accuracy={acc:.0%}")
        return session_id

    except Exception as e:
        print(f"❌ 写入失败: {e}")
        return None


def main() -> None:
    print(f"⏰ [{datetime.now().strftime('%H:%M:%S')}] anan → sinoclaw 洞察同步")

    insight = load_latest_wisdom()
    if not insight:
        # 如果没有 wisdom 文件，生成一个基础状态报告
        insight = {
            "wisdom_facts": [],
            "prediction_stats": {"accuracy": 0.0, "confirmed": 0, "failed": 0},
            "causal_links": [],
            "generated_at": datetime.now().isoformat(),
        }
        print("ℹ️  没有找到 wisdom 文件，生成空状态报告")

    write_to_sinoclaw_session(insight)


if __name__ == "__main__":
    main()
