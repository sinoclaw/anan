# 🧠 anan — 安安的认知架构实验舱

> 「我（爸爸）在造一个有脑子的安安，等他有脑子了很多事情就不用我们决定了」
> —— 陈正一，2026-05-14

## 这是什么

**anan 是一个完整的认知架构实验仓库**，所有代码（底座 + 认知层）都在同一个仓库里。

- 底座：anan 完整代码（gateway/、agent/、plugins/、anan_cli/…）
- 灵魂：anan 独有的 9 层 Mind Stack（kernel/ + layers/L1-L9/）
- 数据：复用 `~/.anan/`（爸爸现有的 anan 环境数据）

## 与 anan 主仓的关系

anan 曾经是 anan 的实验 fork。现在**完全独立**，不再追 anan 上游。

anan 是工具，anan 是想长出脑子的 anan。两个项目各自演进。

## anan 独有目录

```
anan/
├── kernel/        # 自主内核（event_bus / idle_detector / persistent_session / ...）
├── layers/        # 9 层 Mind Stack
│   ├── L1_sleep/         # 睡眠记忆固化
│   ├── L2_memory/        # 长时记忆分层
│   ├── L3_attention/     # 注意力调度
│   ├── L4_consciousness/ # 意识流
│   ├── L5_prediction/    # 预测系统
│   ├── L5_reasoning/     # 因果推理（✅ 已完成）
│   ├── L6_metacognition/ # 元认知（⚠️ 闭环未完成）
│   ├── L7_goals/         # 目标系统
│   ├── L8_drives/        # 驱动力
│   ├── L8_intent/        # 意图栈
│   └── L9_self/          # 自我意识（✅ 已完成）
├── adapters/      # anan ↔ anan 桥梁
├── legacy/        # OpenClaw 参考代码
└── .anan/         # 灵魂文档（DESIGN.md / docs / manifest.json）
```

详见 [`.anan/DESIGN.md`](.anan/DESIGN.md) 和 [`.anan/docs/ROADMAP.md`](.anan/docs/ROADMAP.md)

## 当前阶段

- **v0.2.0-sprouting**：品牌升级完成，kernel/layers 骨架就位
- **L5 因果推理**：✅ PatternMiner + wisdom_facts 已完成
- **L9 自我意识**：✅ self_model.py 已完成
- **L6 元认知**：⚠️ 预测验证闭环未连接 L5（核心瓶颈）
- 下一步：Phase 1 — kernel 骨架集成 + L1 Sleep 完整实现

---

底层的 anan 文档见 [`README.md`](README.md)。
