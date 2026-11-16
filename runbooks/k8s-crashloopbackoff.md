---
title: Kubernetes Pod CrashLoopBackOff & OOMKilled
services: kubernetes
severity_hint: P2
---

# Kubernetes Pod CrashLoopBackOff & OOMKilled

## Overview

`CrashLoopBackOff` means a container keeps exiting and kubelet is applying exponential back-off (10s, 20s, 40s ... up to 5m) before restarting it. The state is a symptom: find the exit reason first. Exit code `137` with reason `OOMKilled` means the container exceeded its memory limit; `1`/`2` usually mean an application error; `143` is SIGTERM; `Liveness probe failed` means the kubelet killed a slow or hung container.

## Symptoms

- `kubectl get pods` shows `CrashLoopBackOff`, `Error`, or climbing `RESTARTS`.
- Events such as `Back-off restarting failed container`, `Liveness probe failed`, `OOMKilled`.
- Deployment stuck with `0/N` available, rollout never completes.

## Step 1: Identify the failing pods and reason

```bash
kubectl get pods -n <NAMESPACE> -o wide
kubectl describe pod <POD> -n <NAMESPACE>
kubectl get pod <POD> -n <NAMESPACE> -o jsonpath='{.status.containerStatuses[*].lastState.terminated}'
```

In `describe`, check `Last State: Terminated`, `Reason`, `Exit Code`, and the `Events` section at the bottom.

## Step 2: Read the logs of the crashed instance

```bash
kubectl logs <POD> -n <NAMESPACE> --previous --tail=200
kubectl logs <POD> -n <NAMESPACE> -c <CONTAINER> --previous
kubectl get events -n <NAMESPACE> --sort-by=.lastTimestamp | tail -n 30
```

Use `--previous`; the current container may not have produced logs yet.

## Step 3: Classify the cause

| Signal | Cause | Action |
| --- | --- | --- |
| `OOMKilled`, exit 137 | Memory limit too low or leak | Step 4a |
| Exception on startup, missing env/secret | Bad config or secret | Step 4b |
| `Liveness probe failed` with healthy-looking logs | Probe too aggressive / slow start | Step 4c |
| `ImagePullBackOff` / `ErrImagePull` | Wrong tag or registry credentials | Verify `image:` and `imagePullSecrets` |
| Crash right after a deploy | Bad release | Step 5 rollback |

## Step 4a: OOMKilled

Compare actual usage with the limit:

```bash
kubectl top pod <POD> -n <NAMESPACE> --containers
kubectl get deployment <DEPLOYMENT> -n <NAMESPACE> -o jsonpath='{.spec.template.spec.containers[*].resources}'
```

Raise the limit as a temporary mitigation (this restarts pods):

```bash
kubectl set resources deployment/<DEPLOYMENT> -n <NAMESPACE> --limits=memory=<NEW_LIMIT> --requests=memory=<NEW_REQUEST>
```

For JVM apps keep heap below the limit: `-XX:MaxRAMPercentage=70`. If usage rises steadily over hours, it is a leak: capture a heap dump before restart and open a ticket for the owning team.

## Step 4b: Configuration problems

```bash
kubectl get configmap <CONFIGMAP> -n <NAMESPACE> -o yaml
kubectl get secret <SECRET> -n <NAMESPACE> -o jsonpath='{.data}' | head -c 200
kubectl describe deployment <DEPLOYMENT> -n <NAMESPACE> | grep -A5 -i "environment"
```

Confirm every referenced ConfigMap/Secret key exists (`CreateContainerConfigError` indicates a missing one).

## Step 4c: Probe failures

Increase `initialDelaySeconds`, `timeoutSeconds`, and `failureThreshold` for slow-starting apps, or add a `startupProbe`. Do not remove the liveness probe entirely.

## Step 5: Roll back a bad release

```bash
kubectl rollout history deployment/<DEPLOYMENT> -n <NAMESPACE>
kubectl rollout undo deployment/<DEPLOYMENT> -n <NAMESPACE>
kubectl rollout status deployment/<DEPLOYMENT> -n <NAMESPACE> --timeout=180s
```

## Node-level checks

If many unrelated pods on one node crash, check `kubectl describe node <NODE>` for `MemoryPressure`/`DiskPressure`, then `kubectl cordon <NODE>` and drain only after confirming capacity elsewhere.

## Verification

Pods `Running` and `READY 1/1` for 10 minutes with `RESTARTS` unchanged; deployment `rollout status` successful; error rate and latency back to baseline.

## Escalation

Escalate to the platform/cluster team if multiple nodes show pressure conditions, the control plane is slow, or the image registry is failing. Escalate to the service owner if logs show application exceptions after a rollback to the last known-good version.

## Rollback

`kubectl rollout undo deployment/<DEPLOYMENT> -n <NAMESPACE> --to-revision=<REVISION>`. To revert a resource change, re-apply the previous manifest from Git rather than hand-editing.
