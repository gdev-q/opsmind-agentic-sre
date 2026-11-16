---
title: Multi-DC Failover Checklist
services: general
severity_hint: P1
---

# Multi-DC Failover Checklist

## Overview

Use this checklist when an entire datacenter or region (DC-A, primary) is degraded and traffic must move to the standby (DC-B). Failover is disruptive and carries data-loss risk for asynchronous replication: it requires an Incident Commander (IC) decision. Do not fail over for a single failing service; fix that service with its own runbook.

## When to fail over

Fail over only if ALL are true:

1. DC-A impact is confirmed by at least two independent signals (network probes, synthetic checks, provider status), not one alert.
2. Estimated time to recover DC-A exceeds 30 minutes or is unknown.
3. DC-B is healthy and has capacity for 100% of traffic.
4. IC and the data-platform on-call have approved, and the decision is logged in the incident channel.

## Step 1: Pre-flight checks on DC-B

```bash
kubectl --context <DC_B_CONTEXT> get nodes
kubectl --context <DC_B_CONTEXT> get pods -A --field-selector=status.phase!=Running | head -n 30
kubectl --context <DC_B_CONTEXT> top nodes
```

Confirm autoscaler headroom and that DC-B certificates and secrets are current.

## Step 2: Check replication state (data-loss window)

- MySQL: on the DC-B replica run `SHOW REPLICA STATUS\G` and record `Seconds_Behind_Source` and the executed GTID set.
- Kafka (MirrorMaker 2): check replicated-topic lag with `kafka-consumer-groups.sh --bootstrap-server <DC_B_BROKER>:9092 --describe --group mirrormaker2-cluster`.
- Redis: `redis-cli -h <DC_B_REDIS> INFO replication` and note `master_repl_offset` and `master_link_status`.
- Elasticsearch: confirm cross-cluster replication follower lag with `curl -s "http://<DC_B_ES>:9200/_ccr/stats?pretty"`.

Record these numbers in the incident log; they define the maximum data loss (RPO).

## Step 3: Freeze writes in DC-A (if reachable)

Enable maintenance mode or scale write-path deployments to zero to prevent split-brain:

```bash
kubectl --context <DC_A_CONTEXT> scale deployment -l tier=write-path --replicas=0 -n <NAMESPACE>
```

If DC-A is unreachable, fence it at the load balancer or network layer instead. NEVER leave both DCs accepting writes.

## Step 4: Promote data stores in DC-B

Order: stateful stores first, then application traffic.

1. DESTRUCTIVE: MySQL: wait for the replica to apply the relay log, then promote.

```sql
STOP REPLICA;
RESET REPLICA ALL;
SET GLOBAL read_only = 0;
SET GLOBAL super_read_only = 0;
```

2. DESTRUCTIVE: Redis replica promotion:

```bash
redis-cli -h <DC_B_REDIS> REPLICAOF NO ONE
```

3. Kafka: point producers and consumers at DC-B bootstrap servers; use MM2 offset translation to restart consumer groups.
4. Elasticsearch: promote follower indices (`POST /<INDEX>/_ccr/unfollow` after closing the index) or switch aliases to DC-B clusters.

## Step 5: Shift traffic

```bash
kubectl --context <DC_B_CONTEXT> scale deployment/<DEPLOYMENT> --replicas=<TARGET> -n <NAMESPACE>
```

Update GSLB/DNS weights to 100% DC-B in steps (10%, 50%, 100%) and watch error rate and latency at each step. Lower DNS TTL to 60s ahead of time where possible; clients may cache longer.

## Step 6: Validate

- Synthetic checks green for login, checkout/critical path, and read/write round trip.
- Error rate < 1% and p95 latency within 1.5x baseline for 15 minutes.
- Queue/lag metrics draining; no duplicate or missing orders in a sampled reconciliation.

## Communication

Post a status update every 15 minutes: current state, RPO impact, customer-facing effect, next action, owner. Notify support, product, and leadership when the failover is declared and when it completes.

## Escalation

Escalate to the IC and Head of Engineering if data-loss exposure is above agreed RPO, if DC-B cannot absorb load, if promotion fails, or if a split-brain is suspected. Engage the cloud/network provider's priority support when a provider fault is the cause.

## Failback / Rollback

Do not failback during the incident. After DC-A is declared healthy: rebuild DC-A data stores as replicas of DC-B (never re-promote a stale primary), verify replication lag near zero, schedule failback in a low-traffic window with the same checklist reversed, and hold a post-incident review. To abort a failover in progress before writes started in DC-B, restore GSLB weights to DC-A and un-fence DC-A.
