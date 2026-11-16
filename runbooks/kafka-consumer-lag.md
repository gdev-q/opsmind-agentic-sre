---
title: Kafka Consumer Lag & Rebalance Storms
services: kafka
severity_hint: P2
---

# Kafka Consumer Lag & Rebalance Storms

## Overview

Consumer lag is the gap between the latest offset of a partition (log-end-offset) and the offset committed by a consumer group. Sustained growth means consumers are slower than producers, stuck, or repeatedly rebalancing. Typical alerts: `kafka_consumergroup_lag > 100000 for 10m`, `CommitFailedException`, `Attempt to heartbeat failed since group is rebalancing`.

## Symptoms

- Lag rising on one or more partitions of a topic while producers are healthy.
- Logs containing `CommitFailedException: Commit cannot be completed since the group has already rebalanced`, `max.poll.interval.ms`, or `Member ... sending LeaveGroup request to coordinator`.
- Consumer group state flapping between `PreparingRebalance` and `CompletingRebalance`.
- Downstream SLA breach (orders, notifications, search indexing delayed).

## Step 1: Describe the consumer group

```bash
kafka-consumer-groups.sh --bootstrap-server <BROKER>:9092 --describe --group <GROUP>
kafka-consumer-groups.sh --bootstrap-server <BROKER>:9092 --describe --group <GROUP> --state
kafka-consumer-groups.sh --bootstrap-server <BROKER>:9092 --describe --group <GROUP> --members --verbose
```

Read the `LAG` column per partition. If only a few partitions lag, suspect a hot key or a poison message. If all lag evenly, suspect consumer throughput or a rebalance loop. A `CONSUMER-ID` of `-` means the partition is unassigned.

## Step 2: Rule out broker-side problems

```bash
kafka-topics.sh --bootstrap-server <BROKER>:9092 --describe --under-replicated-partitions
kafka-topics.sh --bootstrap-server <BROKER>:9092 --describe --unavailable-partitions
kafka-broker-api-versions.sh --bootstrap-server <BROKER>:9092 | head -n 5
```

Under-replicated or offline partitions mean the problem is the cluster, not the consumers: go to Escalation.

## Step 3: Diagnose the rebalance loop

A rebalance is triggered when a consumer misses `max.poll.interval.ms` (default 300000), misses heartbeats (`session.timeout.ms`, default 45000), or pods restart/scale. Check consumer logs for processing time per batch. Common causes:

- Slow downstream call (DB, HTTP) inside the poll loop, so batch time exceeds `max.poll.interval.ms`.
- `max.poll.records` too high for the per-record cost.
- Pods being OOM-killed or CPU-throttled, so heartbeats stop.
- Rolling deployment replacing many consumers at once.

## Step 4: Mitigate

1. Reduce batch pressure on the affected application config, then roll the consumers:
   - `max.poll.records=100` (down from 500)
   - `max.poll.interval.ms=600000` if batch processing legitimately takes minutes
   - `session.timeout.ms=45000`, `heartbeat.interval.ms=15000`
2. Scale consumers out, never beyond the partition count (extra consumers sit idle):

```bash
kubectl scale deployment/<CONSUMER_DEPLOYMENT> --replicas=<N> -n <NAMESPACE>
```

3. Use static membership (`group.instance.id`) to avoid rebalances on pod restarts.
4. If a single poison message blocks a partition, route it to a dead-letter topic in application code, then restart the consumer. Do not skip offsets without owner approval.

## Step 5: Reset offsets (last resort, data-skipping)

Stop ALL consumers in the group first; resets fail on active groups.

```bash
kafka-consumer-groups.sh --bootstrap-server <BROKER>:9092 --group <GROUP> --topic <TOPIC> --reset-offsets --to-datetime <YYYY-MM-DDTHH:mm:SS.sss> --dry-run
kafka-consumer-groups.sh --bootstrap-server <BROKER>:9092 --group <GROUP> --topic <TOPIC> --reset-offsets --to-datetime <YYYY-MM-DDTHH:mm:SS.sss> --execute
```

Always run `--dry-run` first and record the old offsets from `--describe` so the reset can be reverted.

## Verification

Lag trend must be negative for 15 minutes and group state `Stable`. Confirm no `CommitFailedException` in the last 10 minutes of consumer logs.

## Escalation

- Escalate to the Kafka platform team (P1 if lag affects payments/orders) when partitions are under-replicated or offline, the controller is flapping, or lag keeps growing after scaling to partition count.
- Escalate to the owning application team if a poison message or slow downstream dependency is confirmed.

## Rollback

- Config change: revert `max.poll.*` settings via `kubectl rollout undo deployment/<CONSUMER_DEPLOYMENT> -n <NAMESPACE>`.
- Offset reset: re-run `--reset-offsets --to-offset <OLD_OFFSET>` per partition using the offsets saved before the reset (group must be stopped).
