```mermaid
flowchart TD
    subgraph Pre模块核心
        A[PreCore] -->|管理| B[模块注册表]
        A -->|协调| C[工作流引擎]
        A -->|使用| D[配置管理器]
    end
    
    subgraph 功能模块层
        E[目的分析模块] 
        F[决策模块]
        G[计划生成模块]
    end
    
    subgraph 工具层
        H[工具注册表]
        I[工具调度器]
        J1[对话工具]
        J2[信息查询工具]
        J3[...]
    end
    
    subgraph 外部对接层
        K[Chat模块适配器]
        L[其他模块接口]
    end
    
    A -->|加载| E
    A -->|加载| F
    A -->|加载| G
    C -->|调用| I
    H -->|注册| J1
    H -->|注册| J2
    H -->|注册| J3
    I -->|使用| K
    I -->|使用| L
    mermaid