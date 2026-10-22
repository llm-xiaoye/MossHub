# 架构与代码导航

```text
网页 / 本地文件 / Obsidian
  → 原件归档与版本记录
  → Docling / 分格式适配 / 按需 OCR
  → 清洗校验与结构感知父子块
  → BGE-M3 + Milvus dense / Jieba-BM25
  → 融合、重排、等价种子去重与父块扩展
  → 证据预算、支持单元与 DeepSeek Flash
  → SSE、多轮记忆、可追溯原文
```

- backend/raghub/api.py：API 与静态页面入口。
- backend/raghub/ingestion、parsing：受理、归档、解析与来源映射。
- backend/raghub/indexing、retrieval：索引、召回、重排、证据与 tokenizer。
- backend/raghub/chat：聊天请求与记忆相关流程（实际目录以源码为准）。
- backend/raghub/manager.py：统一启停；dispatcher.py、worker_app.py：后台派发与任务执行。
- backend/migrations、backend/alembic：数据库迁移成员。
- frontend/src：资料管理与知识问答桌面界面。
- configs：服务配置、资源锁、站点规则和安装模板。
- scripts：管理、导入和资源准备工具。
- tests：已有功能与边界检查，不全是无外部依赖的单元测试。

SQLite 管理任务、文档版本和聊天状态；Milvus 存子块与检索数据；本地文件保存原件、父块和解析产物。SQLite 事务不能单独保证跨存储发布一致性，因此项目实现版本、发布与恢复流程。

成熟组件提供采集、解析、模型推理、向量检索和队列能力；项目实现受理/恢复、结构与来源适配、父子块、融合与证据组织、版本和状态协调、交互界面。尚未进行的系统性算法对照或效果评测不表述为已完成收益。
