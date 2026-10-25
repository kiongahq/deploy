#!/usr/bin/env bash
# Exercise the real workspace reconciler against a disposable Kind cluster.
set -euo pipefail

namespace=kionga-workloads
name=langflow-smoke
kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f config/crd/
kubectl wait --for=condition=Established crd/kiongaworkspaces.mlaiops.io --timeout=90s

cat <<YAML | kubectl apply -f -
apiVersion: mlaiops.io/v1alpha1
kind: KiongaWorkspace
metadata:
  name: $name
  namespace: $namespace
spec:
  subject: smoke-user
  projectID: smoke-project
  services: [langflow]
  compute: {vcpus: 1, memoryGB: 2, maxVMs: 1}
  storageGB: 1
YAML

kubectl wait --for=jsonpath='{.status.langflowURL}'="http://$name.$namespace.svc:7860" \
  kiongaworkspace/$name -n "$namespace" --timeout=180s
kubectl get deployment,pvc,service "$name" -n "$namespace" >/dev/null 2>&1 || {
  kubectl get deployment,pvc,secret,service -n "$namespace"
  exit 1
}
kubectl get secret "$name-auth" -n "$namespace" >/dev/null

deployment="$(kubectl get deployment "$name" -n "$namespace" -o json)"
python3 -c 'import json,sys
d=json.load(sys.stdin)
spec=d["spec"]; pod=spec["template"]["spec"]
assert spec["replicas"] == 1
assert {c["name"] for c in pod["containers"]} == {"langflow", "kionga-api"}
assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "langflow-smoke"
langflow=next(c for c in pod["containers"] if c["name"] == "langflow")
assert {m["mountPath"] for m in langflow["volumeMounts"]} == {"/workspace"}
assert langflow["startupProbe"]["httpGet"]["path"] == "/health_check"
' <<<"$deployment"

pvc_uid="$(kubectl get pvc "$name" -n "$namespace" -o jsonpath='{.metadata.uid}')"
kubectl patch kiongaworkspace "$name" -n "$namespace" --type=merge -p '{"spec":{"disabled":true}}'
kubectl wait --for=jsonpath='{.status.phase}'=Suspended kiongaworkspace/$name -n "$namespace" --timeout=90s
kubectl wait --for=jsonpath='{.spec.replicas}'=0 deployment/$name -n "$namespace" --timeout=90s
test "$(kubectl get pvc "$name" -n "$namespace" -o jsonpath='{.metadata.uid}')" = "$pvc_uid"

mount_pvc() {
  local pod_name="$1"
  cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $pod_name
  namespace: $namespace
spec:
  restartPolicy: Never
  containers:
    - name: storage-check
      image: busybox:1.37
      command: [sh, -c, 'sleep 3600']
      volumeMounts:
        - {name: workspace, mountPath: /workspace}
  volumes:
    - name: workspace
      persistentVolumeClaim: {claimName: $name}
YAML
  kubectl wait --for=condition=Ready "pod/$pod_name" -n "$namespace" --timeout=120s
}

mount_pvc langflow-storage-write
kubectl exec -n "$namespace" langflow-storage-write -- sh -c 'printf persistent-flow > /workspace/flow-marker'
kubectl delete pod langflow-storage-write -n "$namespace" --wait=true

kubectl patch kiongaworkspace "$name" -n "$namespace" --type=merge -p '{"spec":{"disabled":false}}'
kubectl wait --for=jsonpath='{.spec.replicas}'=1 deployment/$name -n "$namespace" --timeout=90s
restart_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl annotate kiongaworkspace "$name" -n "$namespace" "mlaiops.io/restart-at=$restart_at"
for attempt in $(seq 1 30); do
  observed="$(kubectl get deployment "$name" -n "$namespace" -o jsonpath='{.spec.template.metadata.annotations.mlaiops\.io/restart-at}')"
  if [[ "$observed" == "$restart_at" ]]; then break; fi
  sleep 2
done
test "$observed" = "$restart_at"
test "$(kubectl get pvc "$name" -n "$namespace" -o jsonpath='{.metadata.uid}')" = "$pvc_uid"
kubectl patch kiongaworkspace "$name" -n "$namespace" --type=merge -p '{"spec":{"disabled":true}}'
kubectl wait --for=jsonpath='{.spec.replicas}'=0 deployment/$name -n "$namespace" --timeout=90s
mount_pvc langflow-storage-read
test "$(kubectl exec -n "$namespace" langflow-storage-read -- cat /workspace/flow-marker)" = persistent-flow
kubectl delete pod langflow-storage-read -n "$namespace" --wait=true
echo "Langflow workspace reconciled, suspended, resumed and restarted; its PVC retained file data"
