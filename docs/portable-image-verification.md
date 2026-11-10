# Offline portable-image acceptance

`scripts/test_portable_image.py` qualifies a captured image independently of the
production Docker image/content store. It never starts a production worker or
mounts its database, media, configuration, browser profile or secrets. Run as a
Linux root operator, with at least 25 GiB free, a new private work directory and
a short new runtime directory under `/run`:

```sh
python3 scripts/test_portable_image.py \
  --manifest /absolute/private/artifact/manifest.json \
  --work-dir /absolute/private/new-test-directory \
  --runtime-dir /run/lighthouse-image-test-unique
```

The manifest must be the verified captured-artifact manifest with `image`,
`archive`, `archive_sha256`, `archive_bytes`, `state=archive_verified` and ordered
`layers[].diff_id`. It binds the archive to the manifest's directory. Inputs must
be absolute paths without symlinks. An existing test directory is refused: inspect
the original run and process handles before deciding on any retry.

The tested host had Docker 29.9.2, containerd 2.4.2 and Python 3.12. The tool uses
containerd configuration version 4. It requires `unshare`, `nsenter`, `ctr`, Docker,
containerd and root namespace capabilities. It installs no dependencies.

## Isolation and evidence

- A dedicated containerd has its own configuration, socket, root and state.
  A dedicated Docker daemon explicitly connects to that socket, with separate
  root, exec root, PID, client configuration and socket. Both processes run in
  the same isolated mount/network namespaces. No bridge, iptables manipulation,
  port publishing or network interface beyond loopback is enabled.
- Before loading, Docker reports zero images and containers; the private
  containerd reports zero content blobs. Archive size and SHA-256, loaded image
  identity and all ordered rootfs diff IDs are checked.
- The probe uses network none, read-only root, UID/GID 10001, dropped capabilities,
  no-new-privileges, bounded CPU/memory/PIDs and a temporary tmpfs. It records
  Python distribution versions, Debian package versions/architectures, selected
  runtime file hashes and Node/FFmpeg/FFprobe versions plus binary hashes.
- Both test daemons are stopped in cleanup. Production container identities must
  match the pre-test list. Reports, failure logs and inventory stay in the private
  test directory. A timeout is not permission to restart a second test instance.

These controls follow the separate-state requirements documented for
[multiple Docker daemons](https://docs.docker.com/reference/cli/dockerd/#run-multiple-daemons)
and the explicit root/state/socket settings in
[containerd configuration](https://github.com/containerd/containerd/blob/main/docs/man/containerd-config.toml.5.md).
Multiple-daemon operation is an operator test, not a replacement production setup.

## Actual result on 2026-11-10

Base image `sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414`
passed a genuinely empty Docker/containerd import and offline container start.
The archive SHA-256 is
`69bf7bd9dd7201b049cf89692e794a7f52275003cfcd94e6848bab2eae5f9f9d`.
All 20 layers matched. The inventory contains 48 Python distributions and 450
Debian packages. Provider requests were zero and production container identities
were unchanged. Both test processes exited. No live application data was mounted.

The first attempt is retained as **failed**, not overwritten: the separate Docker
daemon implicitly connected to host containerd. Docker and containerd were then
in different mount namespaces, and the probe failed to execute Python; absolute
executable probes also reported permission failures. Its empty Docker namespace
did not prove an empty containerd content store. The corrected test explicitly
started private containerd and checked both empty stores before loading.

After verifying stopped processes and absence of mounts, temporary Docker and
containerd stores were removed. Only the first attempt's exact test-namespace
image reference was removed from host containerd; no global prune was used. A
follow-up found that namespace empty of images, containers, tasks, leases and
snapshots. The original portable archive, test logs and reports were retained.

This proves captured-base portability on the tested Linux amd64 environment.
It does **not** prove fresh public-source reproducibility, private-image
redistribution safety or a real YouTube request. The two current source-control
modules and pause file are separately hash-bound to the base; see the
[effective release manifest](releases/acquisition-source-controls-v1.json).

## Effective overlay restore test

The portable-image command also accepts `--acquisition-overlay /absolute/bundle`.
Use a bundle containing `manifest.json`, `overlays.tar` and the synthetic
`fixtures/` directory described below. The verifier checks the release's
base image and archive digest before starting the private daemon, then runs
the overlay test on the private socket before shutting it down. The former
production host passed this combined drill.
[Standby run](releases/fenced-standby-image-20261111.json).

The captured base alone predates the source-control change. To validate the
effective release, `scripts/test_acquisition_overlay_bundle.py` restores only
`services/hub/worker.py`, `services/hub/source_controls.py` and
`config/source-controls.json` from a manifest-bound tar into a fresh test folder.
It refuses changed hashes, extra or duplicate members, links and path traversal.
It does not extract to the application or replace current policy. Only these
restored files and three synthetic fixture files are mounted into offline,
read-only UID10001 containers. Test container IDs are recorded and checked before
cleanup, including on client timeout.

```sh
python3 scripts/test_acquisition_overlay_bundle.py \
  --manifest /absolute/private/overlay/manifest.json \
  --archive /absolute/private/overlay/overlays.tar \
  --output /absolute/private/new-overlay-test \
  --test-fixtures /absolute/private/synthetic-fixtures
```

The fixture directory must contain `tests/conftest.py`,
`tests/test_source_controls.py` and synthetic `config/hub.toml`. Their hashes are
recorded in the report. Never substitute a real secret-bearing configuration.

The actual restored overlay bundle passed the file/config probe and all five
focused source-control/Worker tests against the qualified 0722 image. All six
local archive safety tests also passed. Production container IDs were unchanged;
no live database/profile/secrets were mounted and no provider request was made.
Both original failure evidence and successful reports are retained. This is an
isolated restore acceptance, not a production rollback or publisher activation.

## Source build on the migrated host, November 11

Commit `ef53386` built on Linux amd64 from the committed Git tree and the
281 checksum-verified files listed by the two pinned submodules. Docker ran
every COPY and RUN instruction with `--no-cache`. The Ubuntu base image
was present locally. The [build receipt](releases/source-image-verification-20261111.json)
records the base digest, image manifest, image config and test results. The
[dependency inventory](releases/source-image-dependencies-20261111.json)
lists the versions of 366 OS packages and 47 Python distributions installed
in this build. Future builds against changing package repositories can differ.

The initial build passed the upstream integrity check as root. The app UID
could not traverse a submodule directory extracted with mode 0700. A repeat
build with corrected staging directory modes reused the affected cached COPY
layer. The Dockerfile now grants traversal on restrictive submodule directories
and runs the checksum check as UID 10001 at build time.

For an isolated check on a Linux host with a clean checkout and initialized
submodules, run these commands from the repository root. The public example
supplies the test configuration. A first test invocation without this mount
passed 16 tests and failed setup for the other 45 because `hub.toml` was
absent. With the example mounted, all 61 passed. The run covered the four V4
handlers, SQLite claims, crash recovery and synthetic media delivery. Chromium
also launched and rendered a local page with the network disabled.

```sh
git submodule update --init --recursive
python3 scripts/verify_upstream.py
docker build --no-cache -t lighthouse-source-check:local .
proof_dir=$(mktemp -d)
sudo chown 10001:10001 "$proof_dir"
docker run --rm --network none --read-only --user 10001:10001 --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:size=256m,mode=1777 \
  --mount "type=bind,src=$PWD/config/hub.example.toml,dst=/opt/lighthouse-hub/config/hub.toml,readonly" \
  --mount "type=bind,src=$proof_dir,dst=/proof" \
  lighthouse-source-check:local python -m pytest -p no:cacheprovider -q \
  --basetemp=/proof/tmp --junitxml=/proof/result.xml \
  tests/test_worker_type_isolation.py tests/test_acquisition_recovery_faults.py \
  tests/test_download_crash_boundaries.py tests/test_replay_multipart_worker_runtime_v4.py
```

Production still uses the qualified immutable image and private policy/code
overlays. Promotion of the source-built image requires checking those overlays
against its effective runtime.
