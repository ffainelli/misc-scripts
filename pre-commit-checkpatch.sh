#!/bin/sh
exec git diff --cached | scripts/checkpatch.pl --no-signoff -
