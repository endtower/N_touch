# Abyss Core 架构图

## 1. 消息处理主流程（对话路由）

```mermaid
flowchart TD
    Start([用户输入触发]) --> Truck{Truck 触发器<br/>接收用户输入}

    Truck --> UserType{用户身份判定}

    UserType -->|其他人| OtherInput[他人输入]
    UserType -->|主人| MasterInput[主人输入]

    OtherInput --> PFCA
    MasterInput --> PFCA

    PFCA[PFCA 处理<br/>目的规划与对话适配] --> AgentCheck{是否触发<br/>Advance Agent?}

    AgentCheck -->|是| AdvanceAgent[Advance Agent<br/>高级代理介入]
    AgentCheck -->|否| Route

    AdvanceAgent --> Route{路由选择}

    Route -->|社交场景| SocialAgent[Social Agent<br/>社交代理]
    Route -->|常规对话| SelfConv[Self Conversation<br/>自身对话处理]

    SocialAgent --> Loop[多轮循环<br/>迭代处理]
    SelfConv --> Loop

    Loop --> Cond{是否<br/>收尾条件达成?}

    Cond -->|否| Loop
    Cond -->|是| Finalize[收尾处理<br/>生成最终响应]

    Finalize --> Out([输出回复])

    style Truck fill:#f9e79f,stroke:#333,stroke-width:2px
    style PFCA fill:#aed6f1,stroke:#333,stroke-width:2px
    style AdvanceAgent fill:#f5b7b1,stroke:#333,stroke-width:2px
    style SocialAgent fill:#a9dfbf,stroke:#333,stroke-width:2px
    style SelfConv fill:#a9dfbf,stroke:#333,stroke-width:2px
    style Loop fill:#f8c471,stroke:#333,stroke-width:2px
    style Finalize fill:#d7bde2,stroke:#333,stroke-width:2px
```

## 2. 说明

- **Truck 触发器**：系统入口，负责接收并路由用户输入
- **用户身份区分**：他人 / 主人，走同一处理管线但上下文与权限不同
- **PFCA**：所有输入必经的统一处理层（目的规划 / 上下文适配）
- **Advance Agent**：可选介入的高级代理，用于复杂决策
- **Social Agent vs Self Conversation**：二选一的响应生成路径
- **多轮循环**：对话迭代，直到满足收尾条件

---

> 后续将补充：注意力模块、记忆模块、API 层的关联图。
