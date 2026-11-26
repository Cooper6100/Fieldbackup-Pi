#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
mode=$1
shift
if [[ ${1:-} == --help ]]; then
    echo "Usage: sudo ./$mode.sh [--apply] (default: show the proposed operation)"
    exit
fi
[[ $# == 0 || ( $# == 1 && $1 == --apply ) ]] || exit 2

snapshot=/var/lib/fieldbackup-install/previous
config=etc/sd-field-backup.json
units=(sd-field-backup.service fieldbackup-oled.service fieldbackup-sync.timer fieldbackup-sync.service fieldbackup-shutdown.service)
paths=("$config" opt/sd-field-backup opt/fieldbackup-oled opt/fieldbackup-sync opt/fieldbackup-shutdown)
for unit in "${units[@]}"; do paths+=("etc/systemd/system/$unit"); done

if [[ $# == 0 ]]; then
    printf '%s would manage these paths and record their previous state under %s:\n' "$mode" "$snapshot"
    printf '  /%s\n' "${paths[@]}"
    exit
fi
[[ $EUID == 0 ]] || { echo 'Run with sudo'; exit 1; }
exec 9>/run/lock/fieldbackup-install.lock
flock -n 9 || { echo 'Another FieldBackup maintenance operation is active'; exit 1; }

stop_units() {
    local unit state
    for unit in fieldbackup-sync.timer fieldbackup-shutdown.service fieldbackup-sync.service fieldbackup-oled.service sd-field-backup.service; do
        state=$(systemctl show -p LoadState --value "$unit") || return
        if [[ $state != not-found ]]; then systemctl stop "$unit" || return; fi
    done
}

record() {
    local dir=$1 path unit
    mkdir -m 700 "$dir"
    for path in "${paths[@]}"; do
        if [[ -e /$path || -L /$path ]]; then
            (cd / && cp -a --parents -- "$path" "$dir")
        fi
    done
    for unit in "${units[@]}"; do
        systemctl is-enabled "$unit" > "$dir/$unit.enabled" 2>/dev/null || true
        systemctl is-active "$unit" > "$dir/$unit.active" 2>/dev/null || true
    done
}

restore() {
    local dir=$1 path unit
    stop_units || return
    for unit in "${units[@]}"; do
        case $(systemctl is-enabled "$unit" 2>/dev/null) in
            enabled) systemctl disable "$unit" || return ;;
            enabled-runtime) systemctl disable --runtime "$unit" || return ;;
        esac
    done
    for path in "${paths[@]}"; do
        rm -rf -- "/$path" || return
        if [[ -e $dir/$path || -L $dir/$path ]]; then
            cp -a -- "$dir/$path" "/$path" || return
        fi
    done
    systemctl daemon-reload || return
    for unit in "${units[@]}"; do
        if [[ $(<"$dir/$unit.enabled") == enabled ]]; then
            systemctl enable "$unit" || return
        elif [[ $(<"$dir/$unit.enabled") == enabled-runtime ]]; then
            systemctl enable --runtime "$unit" || return
        fi
    done
    for unit in "${units[@]}"; do
        if [[ $(<"$dir/$unit.active") == active ]]; then
            systemctl start "$unit" || return
        fi
    done
}

if [[ $mode == rollback ]]; then
    [[ -f $snapshot/ready ]] || { echo 'No completed installation snapshot'; exit 1; }
    restore "$snapshot"
    mv -T "$snapshot" "$snapshot.rolled-back.$(date +%s)"
    echo 'Previous FieldBackup installation restored.'
    exit
fi

[[ $(uname -m) == aarch64 ]] || { echo 'Requires ARM64'; exit 1; }
[[ $(. /etc/os-release; echo "$VERSION_CODENAME") == trixie ]] || { echo 'Requires Debian Trixie'; exit 1; }
if [[ $mode == install ]]; then
    for path in "${paths[@]}"; do
        [[ ! -e /$path && ! -L /$path ]] || { echo "Existing /$path; use upgrade.sh"; exit 1; }
    done
else
    [[ -f /$config && -f /opt/sd-field-backup/backup.py ]] || { echo 'Existing FieldBackup installation required'; exit 1; }
fi
[[ ! -e $snapshot ]] || { echo "A rollback snapshot already exists at $snapshot. Archive it before applying another installation."; exit 1; }

for program in python3 findmnt lsblk systemctl flock; do command -v "$program" >/dev/null; done
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
python3 - "$mode" "$temporary/config.json" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, "payload/sd-field-backup")
from storage import backup_volume, CONFIG

mode, output = sys.argv[1:]
defaults = json.loads(Path("payload/sd-field-backup.json").read_text())
if mode == "upgrade":
    previous = json.loads(CONFIG.read_text())
    for field in ("clock", "oled"):
        defaults[field] |= previous.pop(field, {})
    previous.setdefault("backup_uuid", previous.get("root_uuid", defaults["backup_uuid"]))
    if "backup_mount" not in previous:
        from storage import mount_info
        previous["backup_mount"] = mount_info(previous["destination"])["target"]
    defaults.update(previous)
with backup_volume(defaults, create=(mode == "install")):
    pass
Path(output).write_text(json.dumps(defaults, indent=2) + "\n")
PY

if python3 -c 'import json,sys; sys.exit(not json.load(open(sys.argv[1]))["oled"]["enabled"])' "$temporary/config.json"; then
    python3 -c 'from luma.oled.device import ssd1306; from PIL import ImageFont'
fi
install -d -m 700 "$(dirname "$snapshot")"
pending=$(mktemp -d "$(dirname "$snapshot")/.pending.XXXXXX")
trap 'rm -rf -- "$temporary" "$pending"' EXIT
record "$pending/state"
touch "$pending/state/ready"
mv -T "$pending/state" "$snapshot"
recover() {
    local result=$?
    trap - ERR
    echo "Installation failed; restoring previous state from $snapshot" >&2
    if restore "$snapshot"; then
        mv -T "$snapshot" "$snapshot.failed.$(date +%s)"
    else
        echo "Automatic restore failed. Snapshot retained at $snapshot." >&2
    fi
    exit "$result"
}
trap recover ERR
if [[ $mode == upgrade ]]; then stop_units; fi
for path in opt/sd-field-backup opt/fieldbackup-oled opt/fieldbackup-sync opt/fieldbackup-shutdown; do
    install -d -m 755 "/$path"
    for source in "payload/${path#opt/}/"*; do
        [[ -f $source ]] || continue
        install -m 755 "$source" "/$path/"
    done
done
install -m 644 "$temporary/config.json" "/$config"
for unit in "${units[@]}"; do
    install -m 644 "payload/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
if [[ $mode == install ]]; then
    systemctl enable --now sd-field-backup.service
    if python3 -c 'import json,sys; sys.exit(not json.load(open(sys.argv[1]))["oled"]["enabled"])' "$temporary/config.json"; then
        systemctl enable --now fieldbackup-oled.service
    fi
else
    # Preserve boot enablement and resume previously running services.
    # The shutdown watchdog requires explicit revalidation after an upgrade.
    systemctl disable fieldbackup-shutdown.service
    for unit in sd-field-backup.service fieldbackup-oled.service fieldbackup-sync.timer; do
        if [[ $(<"$snapshot/$unit.active") == active ]]; then systemctl start "$unit"; fi
    done
fi
trap - ERR
echo "FieldBackup 0.2.0 applied. Rollback snapshot: $snapshot"
