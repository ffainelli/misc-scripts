Kernel master CI
================

This runner fetches Linus's master, pins its SHA in a separate checkout, and
builds and boots each configured platform in sequence. It uses the same
Buildroot output directories and TFTP symlinks as the interactive home lab.
Reserve the lab while it runs: its file lock coordinates instances of this
runner, but manual builds and labgrid/boot_smoke.py calls must also stay idle.

The repository's build-all.sh is a configurable version of ~/bin/build-all.sh.
It accepts BR_DIR, TARGETS, and LINUX_SOURCE. CI supplies those variables, cleans
the kernel build directory, and overrides LINUX_OVERRIDE_SRCDIR on the make
command line, taking precedence over the existing local.mk. Other Buildroot
packages and toolchains remain cached. Your development kernel checkout stays
untouched. CI appends -ci-<12-digit-SHA> to the kernel release and checks that
uname reports the built release to detect accidentally booting an older image.

Run from the repository root with Python 3.11+, Git and the existing Buildroot
build dependencies. Install the local labgrid checkout into its virtualenv;
the Telnet extra is needed for WTI power control even though consoles use raw
TCP. The CI runner itself only uses the Python standard library:

```
~/dev/github/labgrid/.venv/bin/pip install -e "$HOME/dev/github/labgrid[telnet]"
```

Validate and run:

```
python3 kernel-ci/run.py --check
python3 -m unittest discover -s kernel-ci -p 'test_*.py'
python3 kernel-ci/run.py
python3 kernel-ci/run.py --force
```

Edit lab.json for local paths and coverage. Boot tests use these settings:

- `labgrid`: the local source checkout (`~/dev/github/labgrid`).
- `labgrid_python`: its interpreter (`~/dev/github/labgrid/.venv/bin/python`).
- `labgrid_environments`: private YAML files (`~/dev/labgrid-local`). A target
  named `rpi4` with mode `aarch64` uses `rpi4-aarch64.yaml` from this directory.
- `boot_timeout`: maximum seconds for each labgrid pytest session (default 600).
- `tftp_root` and each target's `tftp_images`: existing served image/DTB paths
  to validate before starting a build.

The job no longer uses lab.toml or boot_smoke.py. It runs the checkout's
`examples/boot-smoke/test_smoke.py` through its configured virtualenv and checks
that labgrid was imported from that checkout. `labgrid_boot.py` only launches
pytest and converts its JUnit report into the JSON consumed by the existing
platform summary and kernel-release verifier; it contains no console or
bootloader implementation.

`--check` validates the local interpreter, imports, environment bindings,
profile names, uname coverage, Pi config paths, Buildroot configs and TFTP
images without fetching sources, building, or activating DUTs. Boot tests are
still sequential. They power-cycle the configured outlets and the Pi fixture
updates the shared config.txt. Existing TFTP links must point at these
Buildroot output trees. Do not move those trees without updating the manifest
and TFTP setup.

Keep passwords in `LG_LOGIN_PASSWORD` and `LG_WTI_PASSWORD` in the agent
environment. `BOOTSMOKE_LOGIN_PASSWORD` remains a compatible alias for
`LG_LOGIN_PASSWORD` when the latter is unset. Never store passwords in lab.json.

CI20 has a labgrid boot profile and remains covered by build and boot. RDC
remains build-only in lab.json because its UART boot is not yet validated. Add
its target/mode fields only when ready to enable that hardware coverage.

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
Jenkins pages and console timestamps use America/Los_Angeles (Pacific time,
with automatic daylight saving changes), configured by timezone.groovy.
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
configuration, local labgrid Python sources, the pytest suite, environment YAML
or Buildroot .config also trigger another run, even for the same kernel SHA.

Results include the full upstream SHA, configuration, build logs, Buildroot and
kernel configs, boot logs, normalized results.json, labgrid console/kernel/dmesg
logs, pytest JUnit, and a JUnit build-and-boot result per profile. Jenkins
publishes both platform results and individual pytest checks; target/mode
prefixes keep check names unique. A build failure skips that profile's boot;
other profiles continue. Build-only success does not claim hardware coverage.
Images are served from the existing output trees and are not archived. Save
images separately if long-term binary retention is needed. No automatic flaky
test retries or ignored kernel diagnostics are added.

Exit status: 0 for success or no new revision, 1 for completed build/boot
failures, 2 for setup/infrastructure errors. Jenkins archives available results
even after failures. Every build links to its archived results/output and console,
including the runner log. Polls with no changes produce a summary instead of
JUnit files; the summary and runner log reference the original results when
the completed run was recorded by this version of the runner. Those links
remain subject to Jenkins build retention.

The build description, console, and archived `summary.txt` list each platform's
PASS/FAIL result and totals. Failures identify the build or boot phase, failed
pytest check names, kernel diagnostics, or kernel release mismatch. Missing,
invalid, stale, empty or skipped pytest results cannot produce a passing boot;
all configured checks, including uname and kernel-log scanning, must pass. Detailed
failure reasons also appear in JUnit. `summary.html` provides the escaped build
description content. Polls with no changes explicitly say no platforms ran.

The source clone and completed-revision state live in the configured state
directory; result retention is managed by Jenkins. The initial clone references
the local development repository's objects, so retain that repository. Do not
run git prune in it while CI uses it; run git repack -a in the CI clone to make
it independent before removing or pruning the seed repository.

References: [Jenkins Pipeline syntax](https://www.jenkins.io/doc/book/pipeline/syntax/)
and [Buildroot source overrides](https://buildroot.org/downloads/manual/manual.html#_using_buildroot_during_development).
