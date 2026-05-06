package infrastructure

deny contains msg if {
    input.disk_free_gb < 10
    msg := sprintf("Insufficient disk space: %v GB free (minimum 10 GB required)", [input.disk_free_gb])
}

deny contains msg if {
    input.cpu_load_1m > 2.0
    msg := sprintf("CPU load too high: %v (maximum 2.0 allowed)", [input.cpu_load_1m])
}
