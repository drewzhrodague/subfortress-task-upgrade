# Subfortress Task Worker — upgrade 0.2 (experimental)

Adds a **separate Kubernetes task worker** to the existing Subfortress Researcher deployment. Intended for the current Basement control plane and PostgreSQL 18 cluster. Does not modify existing database tables or erase data.

## Install on Basement

Extract this archive and run as root:

```bash
cd subfortress-task-upgrade
./upgrade-task-worker.sh
```

Requires the existing `/opt/subfortress/harness/subfortress-researcher` installation, PostgreSQL public certificate at `/etc/ssl/certs/postgresql-subfortress.crt`, existing Kubernetes credentials Secret, and access to the cluster (`/etc/kubernetes/admin.conf`). The script appends `[worker]` to the existing `agent.ini` if missing, copies `task_worker.py`, updates the shared ConfigMap, restarts the researcher API, and deploys one task worker. No secrets are included in this archive.

## Smoke test

```bash
KUBECONFIG=/etc/kubernetes/admin.conf kubectl logs -f deployment/subfortress-task-worker -c worker
```

In another terminal create a pending research task:

```bash
runuser -u postgres -- psql -d subfortress -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO agents.tasks (title,description)
VALUES ('Test research worker', 'Explain in three sentences why Kubernetes uses readiness probes.')
RETURNING id, status;
SQL
```

Wait for the task to complete (may take time for Gemma4 inference), then inspect:

```bash
runuser -u postgres -- psql -d subfortress -c \
  "SELECT id,title,status,result FROM agents.tasks ORDER BY created_at DESC LIMIT 3;"
runuser -u postgres -- psql -d subfortress -c \
  "SELECT task_id,status,worker_name,started_at,finished_at FROM agents.task_executions ORDER BY started_at DESC LIMIT 5;"
```

## Recovery test (non-destructive)

1. Create a research task with a long prompt, and observe the worker log for `claimed ...`.
2. Force-delete the worker pod **only after** a claim, for example `kubectl delete pod -l app=subfortress-task-worker --force --grace-period=0` (only in this test cluster).
3. The replacement worker must wait until the original lease expires (default 120 seconds). It then marks the old execution `interrupted`, claims the task in a new execution, and completes it.
4. Inspect `agents.task_executions` to verify the history. A force-deleted pod may continue briefly on a partitioned node; do not use this recovery behavior for side-effecting tasks.

## Safety and limitations

- Task claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, in one database transaction.
- Each execution has a lease; heartbeats extend the lease. An expired lease is reclaimable.
- **At-least-once**, not exactly-once: model inference may repeat after interruption.
- No arbitrary shell, web browsing, infrastructure mutations, or external side-effecting tools are available to the worker.
- Tasks with `agents.operations` in `running` or `unknown` state are blocked from automatic claiming pending manual reconciliation.
- Failed model calls become `failed`; there is no automatic retry policy or maximum-attempt limit yet.
- Checkpoint fields record lifecycle phase, **not** resumable model-generation state.
- The worker currently processes all pending tasks in this dedicated research queue; add task-type routing and authorization before creating other kinds of tasks.
- Task completion updates are guarded by execution status but **not** by a unique lease-owner token on the task row. Do not deploy multiple replicas or allow worker failover on partitioned networks until fencing is implemented. This is a single-replica experimental milestone, not production-grade exactly-once scheduling.
- Dependency installation occurs on each pod start; build a pinned container image for production.
- If the existing ConfigMap is managed by the old `bootstrap-researcher.sh`, running that older bootstrap again will remove `task_worker.py` from the ConfigMap. Integrate this upgrade into the main bootstrap before rerunning the old script.

## GitHub staging

Suggested repo layout:

```text
subfortress/
  README.md
  .gitignore
  researcher/
    agent_service.py
    agent.ini
    task_worker.py
    requirements.txt
    bootstrap-researcher.sh
    upgrade-task-worker.sh
  docs/
    architecture.md
    token-handling.md
```

Keep the real `agent.ini` out of a public repository if it later gains sensitive network information. Prefer `agent.ini.example` and environment/Secret injection. The `.gitignore` in this package is a starting point, not a substitute for reviewing `git diff --cached` before pushing.
