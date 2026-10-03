#!/sbin/sh
# Source this file, then run walt_vendor_layout IMAGE before magiskboot unpack.
# stdout is a comparison manifest, not shell code. Extract the expected files
# with: awk '$1 == "cpio" { print $2 }' layout.txt
#
# Format and unpack filenames verified against pinned Magisk v31.0:
# https://github.com/topjohnwu/Magisk/blob/v31.0/native/src/boot/bootimg.hpp
# https://github.com/topjohnwu/Magisk/blob/v31.0/native/src/boot/bootimg.cpp
# Only raw vendor_boot v4, 2128-byte headers and 108-byte entries are supported.
# This checks the vendor payload layout; it does not authenticate AVB signatures.

_walt_vendor_error() {
    printf 'WALT vendor_boot: %s\n' "$*" >&2
    return 1
}

# Hash exactly the requested range. Block reads keep large DTBs inexpensive;
# byte reads handle the short remainder without relying on GNU dd extensions.
_walt_vendor_hash() (
    set -o pipefail || return 1
    if [ $(( $2 % 512 )) -eq 0 ]; then
        {
            dd if="$1" bs=512 skip=$(( $2 / 512 )) count=$(( $3 / 512 )) 2>/dev/null || exit 1
            dd if="$1" bs=1 skip=$(( $2 + ($3 / 512) * 512 )) count=$(( $3 % 512 )) 2>/dev/null || exit 1
        } | sha256sum | awk '{print $1}'
    else
        dd if="$1" bs=1 skip="$2" count="$3" 2>/dev/null | sha256sum | awk '{print $1}'
    fi
)

walt_vendor_layout() (
    # A subshell keeps caller options, variables and positional arguments intact.
    set -o pipefail || return 1
    LC_ALL=C
    export LC_ALL
    [ "$#" -eq 1 ] || { _walt_vendor_error 'expected one image'; exit 1; }
    image=$1
    [ -f "$image" ] && [ -r "$image" ] || {
        _walt_vendor_error 'image must be a readable regular file'; exit 1;
    }
    image_size=$(wc -c < "$image") || exit 1
    [ "$image_size" -ge 2128 ] || { _walt_vendor_error 'truncated header'; exit 1; }

    # Decode bytes explicitly as little endian; never depend on od host endian.
    header=$(od -An -v -tu1 -N 2128 < "$image" | awk -v total="$image_size" '
        function fail(s) { print "WALT vendor_boot: " s > "/dev/stderr"; exit 1 }
        function u32(o) { return b[o] + 256*b[o+1] + 65536*b[o+2] + 16777216*b[o+3] }
        function align(n) { return int((n + page - 1) / page) * page }
        { for (i=1; i<=NF; i++) b[n++]=$i }
        END {
            if (n != 2128) fail("truncated header read")
            magic=""; for(i=0;i<8;i++) magic=magic sprintf("%c",b[i])
            if (magic != "VNDRBOOT" || u32(8) != 4) fail("expected raw vendor_boot v4")
            page=u32(12); power=2048
            while (power < page && power < 65536) power*=2
            if (page != power || page > 65536) fail("unsupported page size")
            if (u32(2096) != 2128) fail("unsupported header size")
            ramdisk=u32(24); dtb=u32(2100); table=u32(2112)
            count=u32(2116); entry=u32(2120); config=u32(2124)
            if (ramdisk == 0 || count < 1 || count > 1024) fail("invalid ramdisk/entry count")
            if (entry != 108 || table != count * entry) fail("invalid table size")
            rd_off=align(2128); dtb_off=rd_off+align(ramdisk)
            table_off=dtb_off+align(dtb); config_off=table_off+align(table)
            if (config_off+align(config) > total) fail("sections extend beyond image")
            printf "%.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f\n", \
                page,ramdisk,dtb,table,count,entry,config,dtb_off,table_off,config_off, \
                u32(16),u32(20),u32(2076),u32(2096)
        }
    ') || exit 1
    # All tokens above are generated unsigned decimal integers.
    set -- $header
    [ "$#" -eq 14 ] || { _walt_vendor_error 'header decode failed'; exit 1; }
    page=$1; ramdisk_size=$2; dtb_size=$3; table_size=$4; entry_count=$5
    entry_size=$6; config_size=$7; dtb_offset=$8; table_offset=$9
    shift 9
    config_offset=$1; kernel_addr=$2; ramdisk_addr=$3; tags_addr=$4; header_size=$5

    entries=$(od -An -v -tu1 -j "$table_offset" -N "$table_size" < "$image" |
        awk -v count="$entry_count" -v limit="$ramdisk_size" '
        function fail(s) { print "WALT vendor_boot: " s > "/dev/stderr"; exit 1 }
        function u32(o) { return b[o] + 256*b[o+1] + 65536*b[o+2] + 16777216*b[o+3] }
        function hex(o,len, s,j) { s=""; for(j=o;j<o+len;j++) s=s sprintf("%02x",b[j]); return s }
        { for (i=1;i<=NF;i++) b[n++]=$i }
        END {
            if (n != count*108) fail("truncated table read")
            for (i=0;i<count;i++) {
                base=i*108; size=u32(base); offset=u32(base+4); type=u32(base+8)
                if (size < 1 || offset > limit || size > limit-offset) fail("ramdisk outside section")
                if (type > 3) fail("unknown ramdisk type")
                for (j=0;j<i;j++)
                    if (offset < ends[j] && offset+size > starts[j]) fail("overlapping ramdisks")
                starts[i]=offset; ends[i]=offset+size
                name=""; ended=0
                for(j=base+12;j<base+44;j++) {
                    if (b[j] == 0) { ended=1; continue }
                    if (ended) fail("nonzero bytes after ramdisk name terminator")
                    name=name sprintf("%c",b[j])
                }
                if (!ended) fail("unterminated ramdisk name")
                if (name != "" && name !~ /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/)
                    fail("unsafe ramdisk name")
                if (name == "") name="ramdisk"
                if (seen[name]++) fail("duplicate unpack filename")
                # Preserve raw names too: empty and literal ramdisk are distinct metadata.
                lines[i]=sprintf("entry %d %s %d %s %s\ncpio %s.cpio", \
                    i,name,type,hex(base+44,64),hex(base+12,32),name)
            }
            # Emit nothing until every entry has passed validation.
            for(i=0;i<count;i++) print lines[i]
        }
    ') || exit 1

    cmdline_hash=$(_walt_vendor_hash "$image" 28 2048) || exit 1
    name_hash=$(_walt_vendor_hash "$image" 2080 16) || exit 1
    dtb_addr=$(od -An -v -tx1 -j 2104 -N 8 < "$image" | tr -d ' \n') || exit 1
    dtb_hash=$(_walt_vendor_hash "$image" "$dtb_offset" "$dtb_size") || exit 1
    config_hash=$(_walt_vendor_hash "$image" "$config_offset" "$config_size") || exit 1

    printf '%s\n' 'vendor_boot_layout 1' 'magic VNDRBOOT' 'header_version 4'
    printf 'page_size %s\nheader_size %s\nkernel_addr %s\nramdisk_addr %s\ntags_addr %s\n' \
        "$page" "$header_size" "$kernel_addr" "$ramdisk_addr" "$tags_addr"
    printf 'cmdline_sha256 %s\nproduct_name_sha256 %s\ndtb_addr_le %s\n' \
        "$cmdline_hash" "$name_hash" "$dtb_addr"
    printf 'table_size %s\nentry_count %s\nentry_size %s\n' "$table_size" "$entry_count" "$entry_size"
    printf '%s\n' "$entries"
    printf 'dtb_size %s\ndtb_sha256 %s\nbootconfig_size %s\nbootconfig_sha256 %s\n' \
        "$dtb_size" "$dtb_hash" "$config_size" "$config_hash"
)
