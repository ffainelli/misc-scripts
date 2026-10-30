#!/bin/sh
# Compatible with ~/bin/build-all.sh; CI can select targets and pin its source.
set -eu
BR_DIR=${BR_DIR:-/home/florian/dev/buildroot}
TARGETS=${TARGETS:-"arm64/rpi4-b arm/rpi4-b arm/ns mips64/cobalt mips/cobalt mips/mtx1 mips/ci20 x86/rdc"}
for target in $TARGETS; do
    output="$BR_DIR/output/$target"
    test -f "$output/.config"
    echo "-> Building $target"
    if [ -n "${LINUX_SOURCE:-}" ]; then
        # Command-line assignment takes precedence over the existing local.mk.
        # Start fresh so rsync cannot retain files deleted by upstream commits.
	rm -rf "$BR_DIR/target/lib/modules"
	rm -rf "$BR_DIR/build/linux-custom"
        make -C "$BR_DIR" O="$output" "LINUX_OVERRIDE_SRCDIR=$LINUX_SOURCE" linux-dirclean
        make -C "$BR_DIR" O="$output" "LINUX_OVERRIDE_SRCDIR=$LINUX_SOURCE" linux-reconfigure all
    else
        make -C "$BR_DIR" O="$output" linux-reconfigure all
    fi
    echo "-> $target OK"
done
