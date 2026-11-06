#!/bin/sh
# Publish strata-deepseek.service's memory for node_exporter's textfile collector.
#
# Reads the service's cgroup v2 memory.stat every 15 s and writes
# strata_memory_bytes{kind=...} to $DIR/strata.prom (atomically: tmp + mv, as the
# textfile collector requires). Installed as /usr/local/bin/strata-memory and run
# by strata-memory.service; node-exporter reads $DIR via /host (see docker-compose.yml).
#
#   kind="experts"     anon: the ~108 GB of routed experts the CPU computes, plus buffers
#   kind="pinned"      shmem: pinned staging buffers for host<->GPU copies
#   kind="file_cache"  file - shmem: page cache of the model files (reclaimable)
#   kind="kernel"      kernel memory charged to the service
# strata_memory_current_bytes is the cgroup total; strata_service_up is 0 while the
# service is stopped (lake1-gpu media), and the kinds then read 0.
set -eu
DIR=${DIR:-/var/lib/node_exporter/textfile}
CG=${CG:-/sys/fs/cgroup/system.slice/strata-deepseek.service}
INTERVAL=${INTERVAL:-15}
mkdir -p "$DIR"
while :; do
  tmp="$DIR/.strata.prom.$$"
  if [ -r "$CG/memory.stat" ]; then
    awk -v cur="$(cat "$CG/memory.current")" '
      $1=="anon"{anon=$2} $1=="file"{file=$2} $1=="shmem"{shmem=$2} $1=="kernel"{kernel=$2}
      END{
        print "# HELP strata_memory_bytes Memory of strata-deepseek.service by kind (cgroup v2 memory.stat)."
        print "# TYPE strata_memory_bytes gauge"
        printf "strata_memory_bytes{kind=\"experts\"} %.0f\n", anon
        printf "strata_memory_bytes{kind=\"pinned\"} %.0f\n", shmem
        printf "strata_memory_bytes{kind=\"file_cache\"} %.0f\n", file-shmem
        printf "strata_memory_bytes{kind=\"kernel\"} %.0f\n", kernel
        print "# HELP strata_memory_current_bytes Total memory charged to strata-deepseek.service."
        print "# TYPE strata_memory_current_bytes gauge"
        printf "strata_memory_current_bytes %.0f\n", cur
        print "# HELP strata_service_up 1 while strata-deepseek.service has a cgroup."
        print "# TYPE strata_service_up gauge"
        print "strata_service_up 1"
      }' "$CG/memory.stat" > "$tmp"
  else
    {
      echo "# TYPE strata_memory_bytes gauge"
      for k in experts pinned file_cache kernel; do echo "strata_memory_bytes{kind=\"$k\"} 0"; done
      echo "# TYPE strata_memory_current_bytes gauge"
      echo "strata_memory_current_bytes 0"
      echo "# TYPE strata_service_up gauge"
      echo "strata_service_up 0"
    } > "$tmp"
  fi
  chmod 644 "$tmp"
  mv "$tmp" "$DIR/strata.prom"
  sleep "$INTERVAL"
done
