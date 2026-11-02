#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
NS="${SUBFORTRESS_NAMESPACE:-default}"
BASE="${SUBFORTRESS_INSTALL_DIR:-/opt/subfortress/harness/subfortress-researcher}"
CERT=/etc/ssl/certs/postgresql-subfortress.crt
[[ -f "$BASE/agent_service.py" && -f "$BASE/agent.ini" && -f "$BASE/requirements.txt" ]] || { echo "Missing original researcher in $BASE" >&2; exit 1; }
[[ -r "$CERT" ]] || { echo "Missing PostgreSQL certificate" >&2; exit 1; }
command -v kubectl >/dev/null
kubectl -n "$NS" get secret subfortress-researcher-credentials >/dev/null
kubectl -n "$NS" get deployment subfortress-researcher >/dev/null
# Preserve existing INI and append worker settings only when absent.
if ! grep -q '^\[worker\]' "$BASE/agent.ini"; then
  cat >> "$BASE/agent.ini" <<'INI'

[worker]
lease_seconds = 120
heartbeat_seconds = 20
poll_seconds = 5
INI
fi
install -m 0644 "$ROOT/task_worker.py" "$BASE/task_worker.py"
# Reuse existing credential secret and Python dependencies; no password prompts.
kubectl -n "$NS" create configmap subfortress-researcher-files \
  --from-file=agent.ini="$BASE/agent.ini" \
  --from-file=agent_service.py="$BASE/agent_service.py" \
  --from-file=task_worker.py="$BASE/task_worker.py" \
  --from-file=requirements.txt="$BASE/requirements.txt" \
  --from-file=postgresql.crt="$CERT" \
  --dry-run=client -o yaml | kubectl apply -f -
HASH="$(sha256sum "$BASE/agent.ini" "$BASE/agent_service.py" "$BASE/task_worker.py" "$BASE/requirements.txt" "$CERT" | sha256sum | cut -d' ' -f1)"
cat <<YAML | kubectl -n "$NS" apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: subfortress-task-worker
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels: {app: subfortress-task-worker}
  template:
    metadata:
      labels: {app: subfortress-task-worker}
      annotations:
        subfortress/config-version: "$HASH"
    spec:
      terminationGracePeriodSeconds: 15
      securityContext: {runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001}
      initContainers:
        - name: install-python-dependencies
          image: python:3.14-slim
          command: ["/bin/sh", "-ec"]
          args: ['pip install --disable-pip-version-check --no-cache-dir --target /packages -r /etc/subfortress/requirements.txt']
          volumeMounts:
            - {name: files, mountPath: /etc/subfortress, readOnly: true}
            - {name: packages, mountPath: /packages}
      containers:
        - name: worker
          image: python:3.14-slim
          command: ["python", "/etc/subfortress/task_worker.py"]
          env:
            - {name: PYTHONPATH, value: /packages}
            - {name: PYDANTIC_AI_NO_BANNER, value: "1"}
            - name: SUBFORTRESS_DB_PASSWORD
              valueFrom: {secretKeyRef: {name: subfortress-researcher-credentials, key: SUBFORTRESS_DB_PASSWORD}}
          volumeMounts:
            - {name: files, mountPath: /etc/subfortress, readOnly: true}
            - {name: packages, mountPath: /packages, readOnly: true}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: "2", memory: 2Gi}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
      volumes:
        - name: files
          configMap: {name: subfortress-researcher-files, defaultMode: 0444}
        - name: packages
          emptyDir: {}
YAML
# Refresh the original API deployment because it shares the same ConfigMap.
kubectl -n "$NS" rollout restart deployment/subfortress-researcher
kubectl -n "$NS" rollout status deployment/subfortress-researcher --timeout=300s
kubectl -n "$NS" rollout status deployment/subfortress-task-worker --timeout=300s
echo "Task worker deployed. Follow logs with: kubectl -n $NS logs -f deployment/subfortress-task-worker -c worker"
