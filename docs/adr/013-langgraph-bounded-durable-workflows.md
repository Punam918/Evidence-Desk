# ADR-013 — LangGraph bounded durable workflows

Status: selected v2 design, supersedes ADR-007 orchestration mechanism; 2026-10-10.

Use LangGraph StateGraph, synchronous checkpoint durability and AsyncPostgresSaver. Ask/compare have explicit branches and finite limits; a separate brief graph pauses for approval. Domain services still own permissions, billing, provider attempts and effects. Native SDKs remain; LangChain integration wrappers and hosted Agent Server are not prerequisites.

Jobs own admission, dispatch and leases; graphs own node progress. Public SSE remains the PostgreSQL event contract. No raw LangGraph debug/message/checkpoint stream is public. Checkpoint state contains validated primitive IDs/hashes and schema_version, not prompt text, credentials, AuthContext or model hidden reasoning. Dedicated schema/role/migrations and creator-owned thread mappings restrict checkpoint access.

Replay is not exactly-once external execution. Before a provider request mark an attempt issued durably; ambiguous issued attempts after crash terminalize interrupted and settle estimated cost. DB save effect is unique by execution ID; resume rereads authority and ignores untrusted caller control data. No graph checkpoint can revive terminal runs or deleted sources. [Protocol](../03-ai-engineering/langgraph-workflows.md).
