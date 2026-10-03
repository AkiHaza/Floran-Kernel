#!/system/bin/sh
# Inspect the load lists used by Android's vendor/system module loaders.
# A modules.dep entry or hardware alias alone is inventory, not a load request.
# This helper reads metadata only. It never loads modules or writes partitions.

_walt_external_directory() (
  directory=$1
  for name in modules.load modules.dep; do
    [ -f "$directory/$name" ] && [ -r "$directory/$name" ] || {
      echo "WALT: missing readable $directory/$name" >&2
      exit 1
    }
  done
  set -- "$directory/modules.load" "$directory/modules.dep"
  for name in modules.alias modules.softdep modules.blocklist; do
    [ -e "$directory/$name" ] || continue
    [ -f "$directory/$name" ] && [ -r "$directory/$name" ] || exit 1
    set -- "$@" "$directory/$name"
  done
  # An unfamiliar loader may prefer a binary index over the text we inspect.
  for file in "$directory"/modules.*.bin; do
    [ ! -e "$file" ] || {
      echo "WALT: unsupported binary module index: $file" >&2
      exit 1
    }
  done
  awk '
  function canonical(token) {
    sub(/^.*\//, "", token)
    sub(/\.ko(\.[[:alnum:]_+-]+)?$/, "", token)
    gsub(/-/, "_", token)
    return token
  }
  function target(token) {
    token=canonical(token)
    return token == "qcom_cpufreq_hw" || token == "sched_walt"
  }
  function fail(message) {
    print "WALT: " message " in " FILENAME ":" FNR > "/dev/stderr"
    bad=1
    exit 1
  }
  function glob_regex(pattern, result,i,j,c,part) {
    result="^"
    for(i=1;i<=length(pattern);i++) {
      c=substr(pattern,i,1)
      if(c == "*") result=result ".*"
      else if(c == "?") result=result "."
      else if(c == "[") {
        j=i+1
        if(substr(pattern,j,1) == "!") j++
        if(substr(pattern,j,1) == "]") j++
        while(j <= length(pattern) && substr(pattern,j,1) != "]") j++
        if(j > length(pattern)) result=result "\\["
        else {
          part=substr(pattern,i,j-i+1)
          if(substr(part,2,1) == "!") part="[^" substr(part,3)
          result=result part
          i=j
        }
      } else if(c == "\\") {
        if(i == length(pattern)) fail("trailing escape in module alias")
        c=substr(pattern,++i,1)
        result=result "\\" c
      } else if(index(".^$+(){}|",c)) result=result "\\" c
      else result=result c
    }
    return result "$"
  }
  function enqueue(request, parent) {
    if(request == "" || seen[request]++) return
    queue[++tail]=request
    origin[request]=parent
  }
  {
    kind=FILENAME
    sub(/^.*\//,"",kind)
    line=$0
    sub(/\r$/,"",line)
    sub(/[[:space:]]*#.*/,"",line)
    sub(/^[[:space:]]+/,"",line)
    sub(/[[:space:]]+$/,"",line)
    if(line == "") next
    n=split(line,field,/[[:space:]]+/)
    if(kind == "modules.load") {
      # Hardware aliases contain significant punctuation, including hyphens.
      # Keep request spelling for alias matching; normalize graph keys only.
      for(i=1;i<=n;i++) loads[++load_count]=field[i]
    } else if(kind == "modules.dep") {
      if(field[1] !~ /:$/) fail("malformed dependency record")
      sub(/:$/,"",field[1])
      module=canonical(field[1])
      for(i=2;i<=n;i++) deps[module]=deps[module] " " field[i]
    } else if(kind == "modules.alias") {
      if(n != 3 || field[1] != "alias") fail("malformed alias record")
      aliases[++alias_count]=glob_regex(field[2])
      alias_module[alias_count]=field[3]
    } else if(kind == "modules.softdep") {
      if(n < 3 || field[1] != "softdep") fail("malformed soft dependency record")
      module=canonical(field[2])
      state=""
      for(i=3;i<=n;i++) {
        token=field[i]
        if(substr(token,1,4) == "pre:") { state="pre:"; token=substr(token,5) }
        else if(substr(token,1,5) == "post:") { state="post:"; token=substr(token,6) }
        if(token == "") continue
        if(state == "") fail("soft dependency without pre/post section")
        deps[module]=deps[module] " " token
      }
    } else if(kind == "modules.blocklist") {
      if(n != 2 || (field[1] != "blocklist" && field[1] != "blacklist"))
        fail("malformed blocklist record")
      blocked[canonical(field[2])]=1
    }
  }
  END {
    if(bad) exit 1
    # The vendor scripts remove blocklisted entries from the explicit load list.
    # A hard dependency on a blocked built-in still indicates an invalid load
    # plan, so blocklisting does not hide dependencies during traversal.
    for(i=1;i<=load_count;i++)
      if(!blocked[canonical(loads[i])]) enqueue(loads[i],"modules.load")
    for(head=1;head<=tail;head++) {
      request=queue[head]
      module=canonical(request)
      if(target(module)) {
        print "WALT: external load reaches built-in " module " from " origin[request] > "/dev/stderr"
        exit 1
      }
      n=split(deps[module],dependency,/[[:space:]]+/)
      for(i=1;i<=n;i++) if(dependency[i] != "") enqueue(dependency[i],module)
      for(i=1;i<=alias_count;i++)
        if(request ~ aliases[i]) enqueue(alias_module[i],request " (alias)")
    }
  }' "$@" || exit 1
  echo "WALT: external load plan verified: $directory"
)

_walt_external_family() (
  family=$1
  shift
  work=$(mktemp -d "${TMPDIR:-/tmp}/walt-external.XXXXXX") || exit 1
  trap 'rm -rf -- "$work"' EXIT
  trap 'exit 1' HUP INT TERM
  : > "$work/seen"
  found=0
  for root in "$@"; do
    [ -d "$root" ] || continue
    # Resolve a module-root symlink, then recurse without following arbitrary
    # directory symlinks. This handles /system/lib/modules -> /system_dlkm/...
    physical=$(CDPATH= cd -P -- "$root" && pwd -P) || exit 1
    grep -qxF "$physical" "$work/seen" && continue
    printf '%s\n' "$physical" >> "$work/seen"
    find "$physical" -name modules.load -print0 > "$work/loads" || exit 1
    while IFS= read -r -d '' load; do
      _walt_external_directory "${load%/*}" || exit 1
      found=1
    done < "$work/loads"
  done
  [ "$found" = 1 ] || {
    echo "WALT: mount readable $family module load lists before installing" >&2
    exit 1
  }
)

# Optional explicit roots are useful for offline validation of copied metadata.
walt_check_external_modules() {
  case "$#" in
    0)
      _walt_external_family vendor /vendor/lib/modules /vendor_dlkm/lib/modules || return 1
      _walt_external_family system /system/lib/modules /system_dlkm/lib/modules
      ;;
    2)
      _walt_external_family vendor "$1" || return 1
      _walt_external_family system "$2"
      ;;
    *) echo "WALT: expected no arguments or vendor and system metadata roots" >&2; return 1;;
  esac
}
