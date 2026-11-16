---
title: Spring Boot HikariCP Connection Pool Exhaustion
services: springboot, mysql
severity_hint: P1
---

# Spring Boot HikariCP Connection Pool Exhaustion

## Overview

HikariCP hands out a fixed number of JDBC connections (`maximum-pool-size`, default 10). When all are checked out, request threads wait up to `connection-timeout` (default 30000 ms) and then fail with `SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms`. Root causes are leaks (connections not returned), slow queries or long transactions holding connections, an undersized pool for the traffic, or a database at its `max_connections` ceiling.

## Symptoms

- `HikariPool-1 - Connection is not available, request timed out after 30000ms (total=10, active=10, idle=0, waiting=37)`.
- HTTP 500/503 and rising latency on endpoints that touch the database; Tomcat threads blocked in `HikariPool.getConnection`.
- Actuator: `hikaricp.connections.active` equals `hikaricp.connections.max`, `hikaricp.connections.pending` > 0.
- `Apparent connection leak detected` warnings (when leak detection is enabled).
- MySQL: `Too many connections` (error 1040).

## Step 1: Confirm pool saturation

```bash
curl -s http://<POD_IP>:8080/actuator/metrics/hikaricp.connections.active
curl -s http://<POD_IP>:8080/actuator/metrics/hikaricp.connections.pending
curl -s http://<POD_IP>:8080/actuator/metrics/hikaricp.connections.max
curl -s http://<POD_IP>:8080/actuator/metrics/hikaricp.connections.acquire
```

Note `total=`, `active=`, `idle=`, `waiting=` in the Hikari log line for a quick read.

## Step 2: See what the database is doing

```sql
SHOW FULL PROCESSLIST;
SELECT id, user, host, db, command, time, state, LEFT(info, 120) AS query
  FROM information_schema.processlist WHERE command <> 'Sleep' ORDER BY time DESC LIMIT 20;
SELECT trx_id, trx_started, trx_mysql_thread_id, trx_query
  FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;
SHOW STATUS LIKE 'Threads_connected';
SHOW VARIABLES LIKE 'max_connections';
```

Many `Sleep` connections with high `time` and open transactions point to a leak or a transaction held across a slow external call. Many long-running queries point to a missing index or lock contention (`SHOW ENGINE INNODB STATUS\G`).

## Step 3: Find who holds connections

```bash
kubectl exec -it <POD> -n <NAMESPACE> -- jcmd 1 Thread.print > /tmp/threads.txt
grep -c "HikariPool.getConnection" /tmp/threads.txt
grep -B2 -A12 "com.mysql.cj" /tmp/threads.txt | head -n 80
```

Threads parked inside business code while holding a connection (HTTP calls, `Thread.sleep`, message sends inside `@Transactional`) are the offenders.

## Step 4: Mitigate

1. Kill runaway queries/sessions that have been stuck (confirm they are not legitimate batch jobs):

```sql
KILL <PROCESS_ID>;
```

2. Restart the unhealthy pods one at a time to release leaked connections:

```bash
kubectl rollout restart deployment/<DEPLOYMENT> -n <NAMESPACE>
kubectl rollout status deployment/<DEPLOYMENT> -n <NAMESPACE>
```

3. Check the arithmetic before enlarging a pool: `replicas x maximum-pool-size` must stay below MySQL `max_connections` (minus admin/other clients). Then adjust:

```yaml
spring.datasource.hikari.maximum-pool-size: 20
spring.datasource.hikari.connection-timeout: 10000
spring.datasource.hikari.leak-detection-threshold: 20000
spring.datasource.hikari.max-lifetime: 1740000
```

Fail fast with a shorter `connection-timeout` rather than letting threads pile up.

## Step 5: Fix the root cause

- Keep `@Transactional` scopes small; never call remote services inside a transaction.
- Always close resources (`try-with-resources`, `JdbcTemplate`/repositories); enable leak detection to find the stack trace of the leaking borrower.
- Add the missing index for the slow query (`EXPLAIN` it) and set statement/query timeouts.
- Set `max-lifetime` below MySQL `wait_timeout` and any proxy/LB idle timeout.

## Verification

`hikaricp.connections.pending` returns to 0, `active` is well below `max` at peak, no `Connection is not available` errors for 15 minutes, and p95 latency is back to baseline.

## Escalation

Escalate to the DBA team at P1 if MySQL is at `max_connections`, replication lag is growing, or the primary is unresponsive. Escalate to the service-owning team with the thread dump and slow-query list if a code-level leak or long transaction is identified.

## Rollback

Revert pool configuration with `kubectl rollout undo deployment/<DEPLOYMENT> -n <NAMESPACE>` (or the previous ConfigMap revision). Killed sessions roll back their open transactions automatically; verify no partial batch work needs replaying.
