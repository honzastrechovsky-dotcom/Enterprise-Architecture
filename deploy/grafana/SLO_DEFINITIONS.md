# SLO/SLI Definitions - Enterprise Architecture Platform

## Overview

This document defines the Service Level Objectives (SLOs), Service Level Indicators (SLIs), and error budget policies for the Enterprise Architecture Platform. All SLOs are measured over a **30-day rolling window**.

---

## SLI Definitions

### 1. Request Success Rate

| Property | Value |
|---|---|
| **Metric** | `sli:request_success_rate:ratio_rate5m` |
| **Formula** | `1 - (rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]))` |
| **Measurement** | Ratio of non-5xx responses to total responses, computed over 5-minute windows |
| **Data Source** | Prometheus `http_requests_total` counter (FastAPI middleware) |
| **Excludes** | 4xx errors (client errors are not platform failures) |
| **Alert Threshold** | `< 0.99` sustained for 5 minutes triggers `HighErrorRate` (critical) |

### 2. Request Latency P99

| Property | Value |
|---|---|
| **Metric** | `sli:request_latency_p99:seconds` |
| **Formula** | `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))` |
| **Measurement** | 99th percentile of HTTP request duration across all endpoints |
| **Data Source** | Prometheus `http_request_duration_seconds` histogram (FastAPI middleware) |
| **Excludes** | Health check endpoints (`/health`, `/ready`) |
| **Alert Threshold** | `> 2s` sustained for 5 minutes triggers `HighP99Latency` (critical) |

### 3. Availability

| Property | Value |
|---|---|
| **Metric** | `sli:availability:ratio_avg5m` |
| **Formula** | `avg_over_time(up{job="enterprise-agent-platform"}[5m])` |
| **Measurement** | Proportion of successful Prometheus scrapes over 5-minute windows |
| **Data Source** | Prometheus built-in `up` metric for the API scrape target |
| **Alert Threshold** | `< 0.995` sustained for 10 minutes triggers `SLOAvailabilityBreach` (critical) |

---

## SLO Targets

| SLO | Target | Error Budget (30 days) | Error Budget (time) |
|---|---|---|---|
| **Availability** | >= 99.5% | 0.5% of minutes | 3 hours 39 minutes |
| **Latency P99** | <= 2 seconds | N/A (absolute threshold) | N/A |
| **Error Rate** | <= 1% | 1% of total requests | Depends on traffic volume |

### Error Budget Calculation

```
Error Budget = 1 - SLO Target
             = 1 - 0.995
             = 0.005 (0.5%)

Monthly minutes = 30 * 24 * 60 = 43,200 minutes
Budget in minutes = 43,200 * 0.005 = 216 minutes = 3h 36m

Monthly seconds = 30 * 24 * 3600 = 2,592,000 seconds
Budget in seconds = 2,592,000 * 0.005 = 12,960 seconds = 3h 36m
```

### Burn Rate Windows

The platform uses multi-window burn rate alerting (per Google SRE handbook):

| Window | Burn Rate | Time to Exhaust Budget | Alert |
|---|---|---|---|
| 5 minutes | 14.4x | ~2 hours | `SLOErrorBudgetFastBurn` (critical) |
| 30 minutes | 6x | ~6 hours | Confirmation window for fast burn |
| 6 hours | 1x | 30 days | Budget tracking (dashboard only) |

---

## Alert Summary

| Alert Name | Severity | Condition | For Duration |
|---|---|---|---|
| `HighErrorRate` | critical | 5xx rate > 1% | 5m |
| `HighP99Latency` | critical | P99 > 2s | 5m |
| `DiskSpaceHigh` | warning | Available < 15% | 10m |
| `HighCPU` | warning | CPU > 90% | 10m |
| `OOMKills` | critical | Restarts > 3/hr | 5m |
| `LLMHighLatency` | warning | LLM P95 > 10s | 5m |
| `AgentEscalationRate` | warning | Escalation > 30% | 15m |
| `SLOErrorBudgetFastBurn` | critical | 14.4x + 6x burn | 2m |
| `SLOLatencyP99Breach` | warning | P99 > 2s | 10m |
| `SLOAvailabilityBreach` | critical | Availability < 99.5% | 10m |

---

## Escalation Policy

### When Error Budget is Healthy (> 50% remaining)

- Normal development velocity
- Feature work proceeds as planned
- Warning alerts investigated during business hours

### When Error Budget is Depleted (25-50% remaining)

- **Freeze non-critical deployments** until budget recovers
- Prioritize reliability improvements over feature work
- Daily error budget review in standup
- On-call reviews all warning alerts within 30 minutes

### When Error Budget is Critical (< 25% remaining)

- **Full deployment freeze** (emergency fixes only)
- Dedicated incident response team assigned
- All engineering effort redirected to reliability
- Hourly error budget check-ins
- Post-incident review required for every alert

### When Error Budget is Exhausted (0% remaining)

- **Mandatory reliability sprint** (1-2 weeks)
- No feature deployments until budget regenerates
- Root cause analysis for all incidents in the window
- Architecture review with senior engineering
- SLO targets reviewed and adjusted if consistently unachievable

---

## Notification Routing

| Severity | Channel | Response Time |
|---|---|---|
| Critical | Webhook (PagerDuty/Slack) + Email | Immediate (< 5 min) |
| Warning | Email | Within 30 minutes |
| SLO Breach | Dedicated SLO Slack channel | Within 15 minutes |

---

## Recording Rules Reference

All recording rules are defined in `slo-recording-rules.yml` and loaded by Prometheus via the `rule_files` configuration in `prometheus.yml`.

| Rule | Type | Description |
|---|---|---|
| `sli:request_success_rate:ratio_rate5m` | Recording | 5-minute success rate |
| `sli:request_latency_p99:seconds` | Recording | 5-minute P99 latency |
| `sli:request_latency_p95:seconds` | Recording | 5-minute P95 latency |
| `sli:availability:ratio_avg5m` | Recording | 5-minute availability |
| `slo:error_budget_burn_rate:5m` | Recording | 5-minute burn rate |
| `slo:error_budget_burn_rate:30m` | Recording | 30-minute burn rate |
| `slo:error_budget_burn_rate:6h` | Recording | 6-hour burn rate |

---

## Revision History

| Date | Change | Author |
|---|---|---|
| 2026-02-19 | Initial SLO/SLI definitions | Platform Engineering |
