# Architecture — research task worker 0.2

- **Researcher API**: existing Pydantic AI HTTP service, `/ask`, persistent conversations and knowledge.
- **Task worker**: separate CPU-only Kubernetes Deployment, one replica, polls PostgreSQL.
- **Inference**: Gemma4 served by existing llama.cpp Kubernetes Service; inference GPU workload remains on Princess/Sister.
- **Database**: PostgreSQL on Basement with verified TLS; task lifecycle in `agents.tasks` and `agents.task_executions`.

## State transitions

`pending -> running -> completed | failed`

A task in `running` with an expired lease may be reclaimed. Previous running execution rows are marked `interrupted`. New execution attempts get new UUIDs. `agents.operations` with `running` or `unknown` status prevent automatic claim.

## Non-goals

No exactly-once guarantee, no multi-worker fencing, no side-effecting tools, no task priorities, no semantic knowledge search, and no UI in this release. Do not treat heartbeat/lease expiry as proof that a disconnected worker stopped executing.
