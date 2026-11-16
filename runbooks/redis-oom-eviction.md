---
title: Redis OOM & Key Eviction
services: redis
severity_hint: P1
---

# Redis OOM & Key Eviction

## Overview

Redis keeps its dataset in RAM and enforces `maxmemory`. With `maxmemory-policy noeviction`, writes fail with `OOM command not allowed when used memory > 'maxmemory'`. With an eviction policy (`allkeys-lru`, `volatile-lru`, ...) Redis silently deletes keys, causing cache misses and database stampedes. If Redis is the system of record (queues, sessions, locks), eviction means data loss.

## Symptoms

- Application errors: `OOM command not allowed when used memory > 'maxmemory'`, `RedisCommandExecutionException`, `JedisDataException`.
- `evicted_keys` increasing, cache hit ratio dropping, DB load spiking.
- `used_memory` near `maxmemory`; high `mem_fragmentation_ratio` (> 1.5) or the host OOM-killer terminating `redis-server`.

## Step 1: Inspect memory state

```bash
redis-cli -h <HOST> -p 6379 INFO memory
redis-cli -h <HOST> -p 6379 INFO stats | grep -E "evicted_keys|keyspace_hits|keyspace_misses"
redis-cli -h <HOST> -p 6379 CONFIG GET maxmemory
redis-cli -h <HOST> -p 6379 CONFIG GET maxmemory-policy
redis-cli -h <HOST> -p 6379 INFO keyspace
```

Compare `used_memory_human`, `used_memory_rss_human`, `maxmemory_human`, and `mem_fragmentation_ratio`.

## Step 2: Find what is consuming memory

```bash
redis-cli -h <HOST> -p 6379 --bigkeys -i 0.1
redis-cli -h <HOST> -p 6379 --memkeys -i 0.1
redis-cli -h <HOST> -p 6379 MEMORY USAGE <KEY> SAMPLES 0
redis-cli -h <HOST> -p 6379 TTL <KEY>
```

Use `-i 0.1` to throttle scans on production. Look for keys without TTL (`TTL` returns `-1`), huge hashes/lists/streams, unbounded queues, or a new key prefix introduced by a recent release. Replication or client output buffers can also be large: check `INFO clients` and `INFO replication`.

## Step 3: Immediate mitigation

1. If policy is `noeviction` on a cache workload, switch to an eviction policy:

```bash
redis-cli -h <HOST> -p 6379 CONFIG SET maxmemory-policy allkeys-lru
```

2. If the host has headroom, raise the limit (keep at least 25% of RAM free for fork/COW and buffers):

```bash
redis-cli -h <HOST> -p 6379 CONFIG SET maxmemory <BYTES>
```

3. Remove confirmed-disposable big keys with non-blocking deletes (never `DEL` on a large key):

```bash
redis-cli -h <HOST> -p 6379 UNLINK <KEY>
```

4. For high fragmentation: `redis-cli MEMORY PURGE`, or enable `activedefrag yes` via `CONFIG SET`.

DESTRUCTIVE: never run `FLUSHALL` or `FLUSHDB` without written approval from the data owner.

## Step 4: Fix the root cause

- Add TTLs to unbounded keys (`EXPIRE <KEY> <SECONDS>`) and fix the writer to set TTL at write time.
- Cap streams/lists (`XADD ... MAXLEN ~ <N>`, `LTRIM`).
- Right-size the instance or shard with Redis Cluster when the dataset legitimately grew.
- Alert at 80% of `maxmemory` and on `evicted_keys` rate.

## Verification

`used_memory` stable below 85% of `maxmemory`, `evicted_keys` rate flat, application OOM errors gone, hit ratio recovered.

## Escalation

Escalate to the data-platform team immediately (P1) when Redis holds non-cache data (sessions, queues, locks), when the master crashed or failed over, or when memory cannot be reclaimed without deleting live data. Page the application owner for runaway key growth.

## Rollback

`CONFIG SET` changes are runtime only: revert with `CONFIG SET maxmemory <OLD_VALUE>` and `CONFIG SET maxmemory-policy <OLD_POLICY>`, and make permanent changes through configuration management. Deleted keys can only be restored from the latest RDB/AOF backup or a replica that has not yet applied the delete.
