---
title: Elasticsearch Red Cluster / Unassigned Shards
services: elasticsearch
severity_hint: P1
---

# Elasticsearch Red Cluster / Unassigned Shards

## Overview

Cluster health is `red` when at least one primary shard is unassigned (data unavailable for that index), `yellow` when only replicas are unassigned. Common causes: node left the cluster, disk watermark exceeded, allocation filtering/awareness misconfiguration, corrupted shard, or too many failed allocation retries (default 5).

## Symptoms

- `_cluster/health` returns `"status":"red"` with `unassigned_shards > 0`.
- Searches fail with `search_phase_execution_exception` / `NoShardAvailableActionException`.
- Indexing fails with `cluster_block_exception: index [..] blocked by: [TOO_MANY_REQUESTS/12/disk usage exceeded flood-stage watermark, index has read-only-allow-delete block]`.
- Logs: `high disk watermark [90%] exceeded`, `node-left`, `failed to allocate shard`.

## Step 1: Assess

```bash
curl -s "http://<ES_HOST>:9200/_cluster/health?pretty"
curl -s "http://<ES_HOST>:9200/_cat/nodes?v&h=name,ip,node.role,heap.percent,disk.used_percent,cpu,load_1m"
curl -s "http://<ES_HOST>:9200/_cat/indices?v&health=red"
curl -s "http://<ES_HOST>:9200/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason,node" | grep UNASSIGNED
```

## Step 2: Ask Elasticsearch why a shard is unassigned

```bash
curl -s -X GET "http://<ES_HOST>:9200/_cluster/allocation/explain?pretty"
curl -s -X GET "http://<ES_HOST>:9200/_cluster/allocation/explain?pretty" -H 'Content-Type: application/json' -d '{"index":"<INDEX>","shard":0,"primary":true}'
```

Read `unassigned_info.reason` and each node's `deciders` (for example `disk_threshold`, `same_shard`, `max_retry`, `awareness`).

## Step 3: Fix by cause

### Disk watermarks (85% low, 90% high, 95% flood-stage)

1. Free space: delete old/unneeded indices (confirm with the data owner first), or add disk/nodes.

```bash
curl -s -X DELETE "http://<ES_HOST>:9200/<OLD_INDEX>"
```

2. After usage is below the high watermark, clear the read-only block that flood-stage applied:

```bash
curl -s -X PUT "http://<ES_HOST>:9200/_all/_settings" -H 'Content-Type: application/json' -d '{"index.blocks.read_only_allow_delete": null}'
```

### Node left / restarted

Wait for the node to rejoin (`index.unassigned.node_left.delayed_timeout`, default 1m). If it is permanently lost, replicas promote automatically; investigate the host and restart the service.

### Allocation retries exhausted

```bash
curl -s -X POST "http://<ES_HOST>:9200/_cluster/reroute?retry_failed=true"
```

### Replica settings on a single-node or small cluster

```bash
curl -s -X PUT "http://<ES_HOST>:9200/<INDEX>/_settings" -H 'Content-Type: application/json' -d '{"index":{"number_of_replicas":0}}'
```

### No copy of a primary available (last resort)

DESTRUCTIVE: restore from the latest snapshot rather than forcing allocation.

```bash
curl -s "http://<ES_HOST>:9200/_snapshot/<REPO>/_all?pretty"
curl -s -X POST "http://<ES_HOST>:9200/_snapshot/<REPO>/<SNAPSHOT>/_restore" -H 'Content-Type: application/json' -d '{"indices":"<INDEX>"}'
```

Only if no snapshot exists and the owner accepts data loss, allocate an empty or stale primary with `_cluster/reroute` (`allocate_empty_primary` / `allocate_stale_primary` with `accept_data_loss: true`).

## Verification

```bash
curl -s "http://<ES_HOST>:9200/_cluster/health?wait_for_status=green&timeout=60s&pretty"
```

Status `green` (or `yellow` on intentionally replica-less indices), `unassigned_shards: 0`, successful search and index test requests, disk below 80% on all data nodes.

## Escalation

Escalate to the search-platform team at P1 if a primary shard has no surviving copy and no snapshot, if more than one data node is down, if the master is unstable (`master_not_discovered_exception`), or if recovery has not progressed for 30 minutes.

## Rollback

Settings changes: restore with the previous value (`number_of_replicas`, watermark settings via `_cluster/settings`). Deleted indices and force-allocated empty primaries cannot be rolled back except by snapshot restore, so record snapshot names before acting.
