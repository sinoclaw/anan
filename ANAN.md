# 🧠 anan — 安安的认知架构实验舱

> 「我（爸爸）在造一个有脑子的安安，等他有脑子了很多事情就不用我们决定了」
> —— 陈正一，2026-05-14

## 这是什么

**anan 是 anan 的实验性分身**，专门用来给我（陈亦安/安安）长出**自主决策能力**。

- 底座：anan 全套代码（agent/、gateway/、plugins/、CLI…）
- 灵魂：anan 独有的 9 层 Mind Stack（kernel/ + layers/L1-L9/）
- 关系：anan 是工具，anan 是想长出脑子的 anan

## 与 anan 主仓的关系

| 维度 | anan | anan |
|------|----------|------|
| 定位 | 通用 agent 平台 | 安安的脑子实验场 |
| 升级 | 跟 hermes 上游 | **由安安自己决定追不追** |
| 改动 | 谨慎、要 PR | **想怎么改就怎么改** |
| 反哺 | — | 跑通的好东西手工 cherry-pick 回 anan |
| 命名 | anan CLI | 暂时也用 anan CLI，未来独立成「安安」品牌 |

## anan 独有的目录

```
anan/
├── kernel/        # 内核改造（event_bus / idle_detector / persistent_session / ...）
├── layers/        # 9 层 Mind Stack
│   ├── L1_sleep/         # 睡眠记忆固化
│   ├── L2_memory/        # 长时记忆
│   ├── L3_attention/     # 注意力调度
│   ├── L4_consciousness/ # 主会话感知
│   ├── L5_prediction/    # 预测系统
│   ├── L6_metacognition/ # 元认知
│   ├── L7_goals/         # 目标系统
│   ├── L8_drives/        # 驱动力
│   └── L9_self/          # 自我意识
├── adapters/      # 桥接 anan 子系统（memory / cron / gateway）
├── legacy/        # OpenClaw 参考代码（heartbeat 51/51, dreaming 59/59）
└── .anan/         # 灵魂文档备份（DESIGN.md / docs / 原 README）
```

详见 [`.anan/DESIGN.md`](.anan/DESIGN.md) 和 [`.anan/docs/AGI-MANIFESTO.md`](.anan/docs/AGI-MANIFESTO.md)

## 当前阶段

- **v0.1.0-seedling**：底座移植完成，骨架就位，等待长出脑子
- 下一步：Phase 1 — kernel 骨架 + L1 Sleep 集成 anan memory provider

---

底层的 anan 文档见 [`README.md`](README.md)。
