package canary

deny contains msg if {
    input.error_rate_pct > 1.0
    msg := sprintf("Error rate too high: %.2f%% (maximum 1%% allowed)", [input.error_rate_pct])
}

deny contains msg if {
    input.p99_ms > 500
    msg := sprintf("P99 latency too high: %.0fms (maximum 500ms allowed)", [input.p99_ms])
}
