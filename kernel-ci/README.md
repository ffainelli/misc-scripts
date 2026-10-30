Kernel master CI
================

This runner fetches Linus's master, pins its SHA in a separate checkout, and
builds and boots each configured platform in sequence. It uses the same
Buildroot output directories and TFTP symlinks as the interactive home lab.
Reserve the lab while it runs: its file lock coordinates instances of this
runner, but manual builds and boot_smoke.py calls must also stay idle.

The repository's build-all.sh is a configurable version of ~/bin/build-all.sh.
It accepts BR_DIR, TARGETS, and LINUX_SOURCE. CI supplies those variables, cleans
the kernel build directory, and overrides LINUX_OVERRIDE_SRCDIR on the make
command line, taking precedence over the existing local.mk. Other Buildroot
packages and toolchains remain cached. Your development kernel checkout stays
untouched. CI appends -ci-<12-digit-SHA> to the kernel release and checks that
uname reports the built release to detect accidentally booting an older image.

Run from the repository root with Python 3.11+, Git, existing Buildroot build
dependencies, and the requirements.txt dependencies installed:

```
python3 kernel-ci/run.py --check
python3 -m unittest discover -s kernel-ci -p 'test_*.py'
python3 kernel-ci/run.py
python3 kernel-ci/run.py --force
```

Edit lab.json for local paths and coverage. Existing lab.toml and boot_smoke.py
are used directly. Keep any login password in BOOTSMOKE_LOGIN_PASSWORD in the
agent environment. Runs power-cycle the configured DUTs and the Pi boot tester
updates the shared Raspberry Pi config.txt. Existing TFTP links must point at
these Buildroot output trees; preflight checks image existence, but routing and
bootloader behavior are only verified by a live run. Do not move output trees
without updating the TFTP setup. RDC and MIPS Creator CI20 are build-only until boot profiles exist in
lab.toml. CI20 uses output/mips/ci20, initialized with:

```
make -C /home/florian/dev/buildroot O=/home/florian/dev/buildroot/output/mips/ci20 ci20_defconfig
```

The CI20 entry has no target/mode, so CI builds it without attempting a boot.
Add those fields after its lab.toml boot profile and hardware setup are ready.

Jenkins setup
-------------

For this host, run `sh kernel-ci/install-jenkins.sh` in an interactive terminal.
It asks sudo to build and start a localhost-only Docker controller, creates an
admin password in ~/.local/state/kernel-ci/admin-password, registers the job,
copies Java 21 from the controller image for the host agent, and enables a
systemd user service for that agent. It does not mount the Docker socket into
Jenkins. Open http://127.0.0.1:8080/; linux-master is queued automatically on
first installation to register the timer. If unattended startup after reboot is needed, enable lingering for
florian (`sudo loginctl enable-linger florian`). The installer fails if its
named container already exists. Its init script reapplies local admin/node/job
configuration on restart; edit the tracked files to change that configuration.
The supplied pipeline uses this repository's absolute path as its workspace;
do not edit it during a CI run. For a separate SCM workspace, change that path
and configure checkout in the job.

Use a Linux agent labeled home-lab running as florian on the lab host, with the
same build tools, Python dependencies, filesystem permissions, and network
access as an interactive lab session. A controller may run elsewhere or in
Docker; builds and power control run on the host agent. Create a Pipeline job
from this repository with script path kernel-ci/Jenkinsfile. Required Jenkins
plugins are Pipeline, Git, JUnit, and Timestamper. Provide a persistent agent
workspace and state directory. No Jenkins credentials are needed to fetch
public upstream Linux.

The job polls every ten minutes. It tests the latest master observed at the
start of a run; commits arriving during a build are picked up on the next poll.
Intermediate commits may be coalesced. It does not promise a test of every
individual commit. It queues concurrent builds instead of interrupting hardware
tests. Completed failures are remembered as well as successes; use FORCE to
retry them. A crash before completion is retried automatically. Changes to lab
configuration, boot script, or Buildroot .config also trigger another run.

Results include the full upstream SHA, configuration, build logs, Buildroot and
kernel configs, boot logs, boot_smoke JSON and console/dmesg logs, and a JUnit
build-and-boot result per profile. A build failure skips that profile's boot;
other profiles continue. Build-only success does not claim hardware coverage.
Images are served from the existing output trees and are not archived. Save
images separately if long-term binary retention is needed. No automatic flaky
test retries or ignored kernel diagnostics are added.

Exit status: 0 for success or no new revision, 1 for completed build/boot
failures, 2 for setup/infrastructure errors. Jenkins archives available results
even after failures. Polls with no changes produce no JUnit files.

The source clone and completed-revision state live in the configured state
directory; result retention is managed by Jenkins. The initial clone references
the local development repository's objects, so retain that repository. Do not
run git prune in it while CI uses it; run git repack -a in the CI clone to make
it independent before removing or pruning the seed repository.

References: [Jenkins Pipeline syntax](https://www.jenkins.io/doc/book/pipeline/syntax/)
and [Buildroot source overrides](https://buildroot.org/downloads/manual/manual.html#_using_buildroot_during_development).
