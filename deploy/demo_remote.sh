#!/usr/bin/env bash
# Repeat the approved demo setup on the existing Oracle deployment.
set -euo pipefail

action=${1:-status}
course_id=
case "$action" in
  status|reset-start)
    if [ "$#" -gt 1 ]; then echo "Usage: $0 $action" >&2; exit 2; fi
    ;;
  materials|stage|seed-activity)
    if [ "$#" -ne 2 ] || ! [[ "$2" =~ ^[1-9][0-9]*$ ]]; then
      echo "Usage: $0 $action COURSE_ID" >&2
      exit 2
    fi
    course_id=$2
    ;;
  *) echo "Usage: $0 {status|reset-start|materials COURSE_ID|stage COURSE_ID|seed-activity COURSE_ID}" >&2; exit 2 ;;
esac

set -- "$action" \
  --db /var/lib/classroom-bridge/classroom.sqlite \
  --env-file /etc/classroom-bridge.env \
  --assets /var/lib/classroom-bridge/demo-assets-full-20261030 \
  --backup-dir /var/lib/classroom-bridge/backups
if [ -n "$course_id" ]; then set -- "$@" --course-id "$course_id"; fi

ssh oracle sudo -u classroom-bridge /srv/classroom-bridge/venv/bin/python \
  /srv/classroom-bridge/current/deploy/demo_setup.py "$@"
