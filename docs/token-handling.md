# Credentials

Do not commit the PostgreSQL password, API token, private keys, Kubernetes Secret exports, or `/root/.researcher-token`.
The upgrade reuses the existing `subfortress-researcher-credentials` Kubernetes Secret and does not ask for secrets again.
Kubernetes Secrets are not necessarily encrypted at rest; enable etcd encryption before production.
