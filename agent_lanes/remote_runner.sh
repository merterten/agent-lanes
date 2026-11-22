#!/bin/sh
# agent-lanes remote runner: stub/run/status/stop/ps for one lane directory.
set -u

usage() {
    echo "usage: remote_runner.sh stub|run|status|stop|ps LANE_DIR" >&2
    exit 2
}

[ "$#" -ge 2 ] || usage
command=$1
lane_dir=${2%/}
[ -n "$command" ] && [ -n "$lane_dir" ] || usage

name=$(basename "$lane_dir")
case "$name" in
    *[!a-z0-9-]*|'') echo "refused: invalid lane name: $name" >&2; exit 2 ;;
esac
if ! printf '%s\n' "$name" | grep -Eq '^[a-z]{2}-worker-[0-9]{1,3}$'; then
    echo "refused: invalid lane name: $name" >&2
    exit 2
fi

state="$lane_dir/.lane-remote"
status="$state/$name.status"
out="$state/$name.out"
pidfile="$state/$name.pid"
launcher="$state/$name.launch.sh"

ts() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_end() {
    code=$1
    printf 'END rc=%s %s\n' "$code" "$(ts)" >> "$status"
}

case "$command" in
    stub)
        mkdir -p "$lane_dir" || exit 1
        printf 'gitdir: .agent-lanes/git-stub\n' > "$lane_dir/.git" || exit 1
        ;;
    run)
        mkdir -p "$state" || exit 1
        cat > "$lane_dir/.lane-task" || exit 1
        printf 'RUNNING %s\n' "$(ts)" > "$status"
        : > "$out"
        worker=${LANE_WORKER:-oc-worker}
        nice=${LANE_NICE:-19}
        model=${LANE_MODEL:-}
        luxalgo=${LANE_LUXALGO:-0}
        {
            printf '#!/bin/sh\n'
            printf 'LANE_DIR=%s\n' "$(quote "$lane_dir")"
            printf 'WORKER=%s\n' "$(quote "$worker")"
            printf 'TASK_FILE=%s\n' "$(quote "$lane_dir/.lane-task")"
            printf 'OUT=%s\n' "$(quote "$out")"
            printf 'STATUS=%s\n' "$(quote "$status")"
            printf 'MODEL=%s\n' "$(quote "$model")"
            printf 'LUXALGO=%s\n' "$(quote "$luxalgo")"
            printf 'cd "$LANE_DIR" || exit 1\n'
            printf 'set -- $WORKER\n'
            printf 'if [ "$LUXALGO" = 1 ]; then set -- "$@" --luxalgo; fi\n'
            printf 'set -- "$@" --write\n'
            printf 'if [ -n "$MODEL" ]; then set -- "$@" -m "$MODEL"; fi\n'
            printf 'set -- "$@" "$(cat "$TASK_FILE")"\n'
            printf '"$@" > "$OUT" 2>&1\n'
            printf 'rc=$?\n'
            printf 'printf "END rc=%%s %%s\\n" "$rc" "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)" >> "$STATUS"\n'
        } > "$launcher" || exit 1
        chmod +x "$launcher" || exit 1
        launched=0
        if [ "${LANE_NO_SYSTEMD:-0}" != 1 ] && command -v systemd-run >/dev/null 2>&1; then
            if systemd-run --user --collect --unit "agent-lane-$name" \
                -p "Nice=$nice" -- "$launcher" >/dev/null 2>&1; then
                launched=1
            fi
        fi
        if [ "$launched" -ne 1 ]; then
            if command -v nice >/dev/null 2>&1; then
                setsid nohup nice -n "$nice" "$launcher" >/dev/null 2>&1 &
            else
                setsid nohup "$launcher" >/dev/null 2>&1 &
            fi
            echo $! > "$pidfile"
        fi
        ;;
    status)
        if [ -f "$status" ]; then
            cat "$status"
        else
            echo "no status for $name"
        fi
        ;;
    stop)
        if command -v systemctl >/dev/null 2>&1; then
            systemctl --user stop "agent-lane-$name" >/dev/null 2>&1 || true
        fi
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            kill -TERM "-$pid" >/dev/null 2>&1 || kill "$pid" >/dev/null 2>&1 || true
            rm -f "$pidfile"
        fi
        if [ -f "$status" ] && ! grep -q '^END ' "$status" 2>/dev/null; then
            write_end 143
        fi
        ;;
    ps)
        if [ -d "$state" ]; then
            for item in "$state"/*.status; do
                [ -f "$item" ] || continue
                printf '%s %s\n' "$(basename "$item" .status)" "$(tail -n 1 "$item")"
            done
        fi
        ;;
    *)
        usage
        ;;
esac
