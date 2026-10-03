#!/system/bin/sh
# Source this file from AnyKernel's BusyBox ash after unpacking every ramdisk
# fragment. This function edits only ROOT; the caller owns repacking/flashing.

_walt_metadata_filter() {
    awk -v kind="$1" '
    function is_walt_module(token) {
        sub(/:$/, "", token)
        sub(/^.*\//, "", token)
        sub(/\.ko(\.[[:alnum:]_+-]+)?$/, "", token)
        gsub(/_/, "-", token)
        return token == "qcom-cpufreq-hw" || token == "sched-walt"
    }
    function has_reference(text) {
        return text ~ /(^|[^[:alnum:]_-])(qcom[-_]cpufreq[-_]hw|sched[-_]walt)(\.ko(\.[[:alnum:]_+-]+)?|[^[:alnum:]_-]|$)/
    }
    function fail() {
        print "WALT: unsupported relevant record in " FILENAME ":" FNR > "/dev/stderr"
        exit 1
    }
    {
        original = $0
        body = original
        sub(/\r$/, "", body)
        if (kind == "script") {
            # Do not mistake a # inside a quoted pathname for a comment.
            if (body ~ /^[[:space:]]*#/) next
            if (has_reference(body)) script_reference = 1
            if (body ~ /(^|[^[:alnum:]_])(insmod|modprobe)([^[:alnum:]_]|$)/) script_loader = 1
            next
        }
        comment = ""
        if (match(body, /[[:space:]]*#/)) {
            comment = substr(body, RSTART)
            body = substr(body, 1, RSTART - 1)
        }
        sub(/^[[:space:]]+/, "", body)
        sub(/[[:space:]]+$/, "", body)
        if (body == "") { print original; next }
        count = split(body, word, /[[:space:]]+/)
        if (kind == "unknown") {
            if (has_reference(body)) fail()
            next
        }
        if (kind == "load") {
            for (i = 1; i <= count; i++) {
                if (is_walt_module(word[i])) {
                    if (count != 1) fail()
                    next
                }
            }
            if (has_reference(body)) fail()
            print original
            next
        }
        if (kind == "dep") {
            if (is_walt_module(word[1])) {
                if (word[1] !~ /:$/) fail()
                next
            }
            changed = 0
            output = word[1]
            for (i = 2; i <= count; i++) {
                if (is_walt_module(word[i])) changed = 1
                else output = output " " word[i]
            }
            if (changed && word[1] !~ /:$/) fail()
            print changed ? output comment : original
            next
        }
        if (kind == "alias") {
            if (count == 3 && word[1] == "alias") {
                if (!is_walt_module(word[3])) print original
            } else {
                if (has_reference(body)) fail()
                print original
            }
            next
        }
        if (kind == "options" || kind == "blocklist") {
            # AOSP uses bare module names in some files; modprobe also accepts
            # the options/blacklist/blocklist directive forms.
            subject = 1
            if (word[1] == "options" || word[1] == "blacklist" || word[1] == "blocklist") subject = 2
            if (!is_walt_module(word[subject])) print original
            next
        }
        if (kind == "softdep") {
            if (count < 2 || word[1] != "softdep") {
                if (has_reference(body)) fail()
                print original
                next
            }
            if (is_walt_module(word[2])) next
            changed = 0
            for (i = 3; i <= count; i++)
                if (is_walt_module(word[i])) changed = 1
            if (!changed) { print original; next }
            before = after = ""
            section = ""
            seen_pre = seen_post = 0
            for (i = 3; i <= count; i++) {
                if (word[i] == "pre:") {
                    if (seen_pre++) fail()
                    section = "pre"
                } else if (word[i] == "post:") {
                    if (seen_post++) fail()
                    section = "post"
                } else {
                    if (section == "") fail()
                    if (!is_walt_module(word[i])) {
                        if (section == "pre") before = before " " word[i]
                        else after = after " " word[i]
                    }
                }
            }
            # Empty soft dependencies are omitted; unrelated dependencies stay.
            if (before == "" && after == "") next
            output = "softdep " word[2]
            if (before != "") output = output " pre:" before
            if (after != "") output = output " post:" after
            print output comment
        }
    }
    END {
        if (kind == "script" && script_reference && script_loader) {
            print "WALT: direct or scripted module loading needs review: " FILENAME > "/dev/stderr"
            exit 1
        }
    }' "$2"
}

walt_patch_ramdisk() (
    # A subshell keeps traps, cwd, and temporary variables out of the caller.
    if [ "$#" -ne 1 ]; then
        echo "WALT: expected one real unpacked ramdisk directory" >&2
        exit 1
    fi
    root_arg=$1
    while [ "${root_arg%/}" != "$root_arg" ] && [ "$root_arg" != / ]; do
        root_arg=${root_arg%/}
    done
    if [ ! -d "$root_arg" ] || [ -L "$root_arg" ]; then
        echo "WALT: expected one real unpacked ramdisk directory" >&2
        exit 1
    fi
    root=$(CDPATH= cd -P -- "$root_arg" && pwd -P) || exit 1
    case "$root" in
        /) echo "WALT: refusing filesystem root" >&2; exit 1 ;;
    esac
    work=$(mktemp -d "${TMPDIR:-/tmp}/walt-ramdisk.XXXXXX") || exit 1
    trap 'while IFS= read -r -d "" staged; do rm -f -- "$staged"; done < "$work/stages"; rm -rf -- "$work"' EXIT
    trap 'exit 1' HUP INT TERM
    : > "$work/stages"
    # BusyBox find does not follow symlinks unless -L is requested. NUL records
    # allow spaces, tabs, and newlines in fragment and module directory names.
    find "$root" -print0 > "$work/tree" || exit 1
    : > "$work/plan"
    number=0
    while IFS= read -r -d '' entry; do
        name=${entry##*/}
        if [ -L "$entry" ]; then
            # Android recovery contains these partition aliases even when it
            # has no modules. Preserve the exact conventional path/target pair;
            # find does not follow it, and this helper never reads its target.
            # A different target, a nested lookalike or a modules link still
            # goes through the refusal checks below.
            link_target=$(readlink -- "$entry") || exit 1
            case "${entry#"$root"/}:$link_target" in
                bin:/system/bin|etc:/system/etc|product:/system/product|system_ext:/system/system_ext|\
                bugreports:/data/user_de/0/com.android.shell/files/bugreports|d:/sys/kernel/debug|\
                odm/app:/vendor/odm/app|odm/bin:/vendor/odm/bin|odm/etc:/vendor/odm/etc|\
                odm/firmware:/vendor/odm/firmware|odm/framework:/vendor/odm/framework|\
                odm/lib:/vendor/odm/lib|odm/lib64:/vendor/odm/lib64|odm/overlay:/vendor/odm/overlay|\
                odm/priv-app:/vendor/odm/priv-app|odm/usr:/vendor/odm/usr|\
                odm_dlkm/etc:/odm/odm_dlkm/etc|vendor_dlkm/etc:/vendor/vendor_dlkm/etc)
                    continue ;;
            esac
            # Android absolute targets may be absent in the installer namespace.
            # Never use -d alone to decide whether a module search path is safe:
            # it would silently accept a dangling lib/modules or partition link.
            case "$name" in
                modules|lib|lib64|usr|system|system_root|vendor|odm|system_dlkm|vendor_dlkm|odm_dlkm|first_stage_ramdisk)
                    echo "WALT: refusing module-path symlink: $entry" >&2
                    exit 1 ;;
            esac
            case "$entry" in
                "$root"/modules/*|"$root"/*/modules/*)
                    echo "WALT: refusing symlink inside module tree: $entry" >&2
                    exit 1 ;;
            esac
            case "$name" in
                modules.*|*.rc|*.sh)
                    echo "WALT: refusing metadata/script symlink: $entry" >&2
                    exit 1 ;;
            esac
            if [ -d "$entry" ]; then
                echo "WALT: refusing directory symlink: $entry" >&2
                exit 1
            fi
            continue
        fi
        case "$name" in
            modules.*)
                if [ ! -f "$entry" ]; then
                    echo "WALT: metadata is not a regular file: $entry" >&2
                    exit 1
                fi
                # .modinfo is deliberately binary; other indexes must be text,
                # including unknown indexes that require a relevance check.
                if [ "$name" != modules.builtin.modinfo ]; then
                    LC_ALL=C tr -d '\000' < "$entry" > "$work/text" || exit 1
                    if ! cmp -s -- "$entry" "$work/text"; then
                        echo "WALT: non-text module metadata: $entry" >&2
                        exit 1
                    fi
                fi
                case "$name" in
                    *.bin)
                        echo "WALT: binary module index requires rebuilding: $entry" >&2
                        exit 1 ;;
                    modules.load*) kind=load ;;
                    modules.dep) kind=dep ;;
                    modules.alias) kind=alias ;;
                    modules.softdep) kind=softdep ;;
                    modules.options) kind=options ;;
                    modules.blocklist) kind=blocklist ;;
                    # These files describe built-ins and do not request loading.
                    modules.builtin|modules.builtin.modinfo) continue ;;
                    *)
                        _walt_metadata_filter unknown "$entry" > /dev/null || exit 1
                        continue ;;
                esac
                number=$((number + 1))
                # Stage beside the destination so mv is a same-filesystem
                # rename, including when fragments live on different mounts.
                staged=$(mktemp "${entry%/*}/.walt-metadata.XXXXXX") || exit 1
                if ! printf '%s\0' "$staged" >> "$work/stages"; then
                    rm -f -- "$staged"
                    exit 1
                fi
                # Preserve ownership and restore even read-only permissions.
                # In-place truncation could also modify an external hardlink.
                cp -p -- "$entry" "$staged" || exit 1
                file_mode=$(stat -c '%a' -- "$entry") || exit 1
                chmod u+w -- "$staged" || exit 1
                _walt_metadata_filter "$kind" "$entry" > "$staged" || exit 1
                chmod "$file_mode" -- "$staged" || exit 1
                if ! cmp -s -- "$entry" "$staged"; then
                    printf '%s\0%s\0' "$entry" "$staged" >> "$work/plan" || exit 1
                fi ;;
            *.rc|*.sh)
                if [ -f "$entry" ]; then
                    LC_ALL=C tr -d '\000' < "$entry" > "$work/text" || exit 1
                    if ! cmp -s -- "$entry" "$work/text"; then
                        echo "WALT: non-text startup script: $entry" >&2
                        exit 1
                    fi
                    _walt_metadata_filter script "$entry" > /dev/null || exit 1
                fi ;;
        esac
    done < "$work/tree"

    # No metadata changes are made until every fragment has passed preflight.
    while IFS= read -r -d '' entry && IFS= read -r -d '' staged; do
        if [ -L "$entry" ] || [ ! -f "$entry" ]; then
            echo "WALT: metadata changed during patching: $entry" >&2
            exit 1
        fi
        mv -f -- "$staged" "$entry" || exit 1
        echo "WALT: patched ${entry#"$root"/}"
    done < "$work/plan"
    echo "WALT: ramdisk module metadata verified"
)
