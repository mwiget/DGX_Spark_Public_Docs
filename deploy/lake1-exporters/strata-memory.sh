#!/bin/sh
# Publish the running Strata LLM's memory for node_exporter's textfile collector.
#
# lake1 serves one of strata-qwen.service / strata-deepseek.service on :8091 (lake1-gpu
# switches them). Reads the running one's cgroup v2 memory.stat every 15 s and writes
# strata_memory_bytes{kind=...} to $DIR/strata.prom (atomically: tmp + mv, as the
# textfile collector requires). Installed as /usr/local/bin/strata-memory and run
# by strata-memory.service; node-exporter reads $DIR via /host (see docker-compose.yml).
#
#   kind="experts"     anon: the routed experts the CPU computes, plus buffers (~108 GB DeepSeek)
#   kind="pinned"      shmem: pinned staging buffers for host<->GPU copies
#   kind="file_cache"  file - shmem: page cache of the model files (reclaimable)
#   kind="kernel"      kernel memory charged to the service
# strata_memory_current_bytes is the cgroup total; strata_service_up is 0 while no
# Strata unit runs (lake1-gpu media), and the kinds then read 0. Every series carries
# unit="strata-qwen|strata-deepseek" (unit="none" while stopped).
set -eu
DIR=${DIR:-/var/lib/node_exporter/textfile}
UNITS=${UNITS:-strata-qwen strata-deepseek}
INTERVAL=${INTERVAL:-15}
mkdir -p "$DIR"
while :; do
  tmp="$DIR/.strata.prom.$$"
  unit=none CG=
  for u in $UNITS; do
    if [ -r "/sys/fs/cgroup/system.slice/$u.service/memory.stat" ]; then
      unit=$u CG=/sys/fs/cgroup/system.slice/$u.service; break
    fi
  done
  if [ -n "$CG" ]; then
    awk -v cur="$(cat "$CG/memory.current")" -v u="$unit" '
      $1=="anon"{anon=$2} $1=="file"{file=$2} $1=="shmem"{shmem=$2} $1=="kernel"{kernel=$2}
      END{
        print "# HELP strata_memory_bytes Memory of the running Strata unit by kind (cgroup v2 memory.stat)."
        print "# TYPE strata_memory_bytes gauge"
        printf "strata_memory_bytes{unit=\"%s\",kind=\"experts\"} %.0f\n", u, anon
        printf "strata_memory_bytes{unit=\"%s\",kind=\"pinned\"} %.0f\n", u, shmem
        printf "strata_memory_bytes{unit=\"%s\",kind=\"file_cache\"} %.0f\n", u, file-shmem
        printf "strata_memory_bytes{unit=\"%s\",kind=\"kernel\"} %.0f\n", u, kernel
        print "# HELP strata_memory_current_bytes Total memory charged to the running Strata unit."
        print "# TYPE strata_memory_current_bytes gauge"
        printf "strata_memory_current_bytes{unit=\"%s\"} %.0f\n", u, cur
        print "# HELP strata_service_up 1 while a Strata unit has a cgroup."
        print "# TYPE strata_service_up gauge"
        printf "strata_service_up{unit=\"%s\"} 1\n", u
      }' "$CG/memory.stat" > "$tmp"
  else
    {
      echo "# TYPE strata_memory_bytes gauge"
      for k in experts pinned file_cache kernel; do echo "strata_memory_bytes{unit=\"none\",kind=\"$k\"} 0"; done
      echo "# TYPE strata_memory_current_bytes gauge"
      echo "strata_memory_current_bytes{unit=\"none\"} 0"
      echo "# TYPE strata_service_up gauge"
      echo "strata_service_up{unit=\"none\"} 0"
    } > "$tmp"
  fi
  chmod 644 "$tmp"
  mv "$tmp" "$DIR/strata.prom"
  sleep "$INTERVAL"
done
