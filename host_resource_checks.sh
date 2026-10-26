#!/usr/bin/env sh
# Sourced by setup.sh. Checks are advisory because Minecraft resource needs
# depend on each server's configuration, mods, and player load.

read_available_memory_mib() {
  meminfo_path=$1
  [ -r "$meminfo_path" ] || return 1

  available_kib=$(sed -n 's/^MemAvailable:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*kB$/\1/p' "$meminfo_path" | sed -n '1p')
  case "$available_kib" in
    ''|*[!0-9]*) return 1 ;;
  esac

  printf '%s\n' "$((available_kib / 1024))"
}

read_available_disk_mib() {
  data_path=$1
  available_kib=$(df -Pk --output=avail "$data_path" 2>/dev/null | tail -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  case "$available_kib" in
    ''|*[!0-9]*) return 1 ;;
  esac

  printf '%s\n' "$((available_kib / 1024))"
}

read_docker_memory_limit_mib() {
  memory_bytes=$1
  case "$memory_bytes" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$memory_bytes" -gt 0 ] || return 1
  printf '%s\n' "$((memory_bytes / 1048576))"
}

check_host_resources() {
  meminfo_path=$1
  data_path=$2
  recommended_memory_mib=$3
  critical_memory_mib=$4
  recommended_disk_mib=$5
  critical_disk_mib=$6
  docker_memory_bytes=${7:-}
  resource_error=false

  for threshold in "$recommended_memory_mib" "$critical_memory_mib" "$recommended_disk_mib" "$critical_disk_mib"; do
    case "$threshold" in
      ''|*[!0-9]*)
        echo "Resource-check thresholds must be non-negative integers." >&2
        return 1
        ;;
    esac
  done

  if available_memory_mib=$(read_available_memory_mib "$meminfo_path"); then
    if [ "$available_memory_mib" -lt "$critical_memory_mib" ]; then
      echo "ERROR: Host has only ${available_memory_mib} MiB memory available; at least ${critical_memory_mib} MiB is required to continue setup." >&2
      resource_error=true
    elif [ "$available_memory_mib" -lt "$recommended_memory_mib" ]; then
      echo "WARNING: Host has ${available_memory_mib} MiB memory available; ${recommended_memory_mib} MiB or more is recommended before assigning Minecraft servers." >&2
    else
      echo "Host memory: ${available_memory_mib} MiB available (recommended minimum ${recommended_memory_mib} MiB)."
    fi
  else
    echo "WARNING: Could not read MemAvailable from $meminfo_path; skipping host-memory estimate." >&2
  fi

  if [ -n "$docker_memory_bytes" ]; then
    if docker_memory_mib=$(read_docker_memory_limit_mib "$docker_memory_bytes"); then
      if [ "$docker_memory_mib" -lt "$critical_memory_mib" ]; then
        echo "ERROR: Docker daemon reports a memory limit of ${docker_memory_mib} MiB; at least ${critical_memory_mib} MiB is required to continue setup." >&2
        resource_error=true
      elif [ "$docker_memory_mib" -lt "$recommended_memory_mib" ]; then
        echo "WARNING: Docker daemon reports a memory limit of ${docker_memory_mib} MiB; ${recommended_memory_mib} MiB or more is recommended before assigning Minecraft servers." >&2
      else
        echo "Docker daemon memory limit: ${docker_memory_mib} MiB."
      fi
    else
      echo "WARNING: Could not parse Docker daemon memory limit '$docker_memory_bytes'; skipping daemon-memory check." >&2
    fi
  else
    echo "WARNING: Docker daemon did not report a memory limit; host memory is checked separately." >&2
  fi

  if available_disk_mib=$(read_available_disk_mib "$data_path"); then
    if [ "$available_disk_mib" -lt "$critical_disk_mib" ]; then
      echo "ERROR: Server-data filesystem has only ${available_disk_mib} MiB free; at least ${critical_disk_mib} MiB is required to continue setup." >&2
      resource_error=true
    elif [ "$available_disk_mib" -lt "$recommended_disk_mib" ]; then
      echo "WARNING: Server-data filesystem has ${available_disk_mib} MiB free; ${recommended_disk_mib} MiB or more is recommended for Minecraft worlds and snapshots." >&2
    else
      echo "Server-data disk: ${available_disk_mib} MiB free (recommended minimum ${recommended_disk_mib} MiB)."
    fi
  else
    echo "WARNING: Could not determine free space for $data_path; skipping disk-space estimate." >&2
  fi

  [ "$resource_error" = false ]
}
