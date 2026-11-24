# SMART and RAID setup

This guide gets disk-health (SMART) data and the optional md RAID panel showing in the dashboard. It takes about 15 minutes. Commands assume Debian or Ubuntu with systemd; adapt the package commands for other distros.

## 1. Why it is set up this way

Reading SMART data needs root and raw access to the disk devices (`/dev/sda` and friends). The dashboard container deliberately has **neither**: it runs as an unprivileged user, read-only, with all capabilities dropped, and it reaches Docker only through a filtered socket proxy. Handing it `--privileged` or `/dev/sdX` just to read temperatures would undo that.

So the work is split in two:

```
 host (root)                                  container (unprivileged)
┌─────────────────────────────────┐          ┌──────────────────────────────────┐
│ dashboard-smart.timer (10 min)  │          │ dashboard                        │
│  └─ dashboard-smart.service     │          │  reads /host/smart/smart.json    │
│      └─ dashboard-smart-collect │  JSON    │  (bind mount, READ-ONLY)         │
│          runs `smartctl -j -a`  ├─────────►│  shows it, flags it if too old   │
│          writes smart.json      │          │                                  │
└─────────────────────────────────┘          └──────────────────────────────────┘
```

* A root job on the host runs `smartctl`, and writes `/var/lib/dashboard/smart.json` (atomically, so the container never sees a half-written file).
* The container mounts that directory **read-only** and only ever reads the file.
* The collector is sandboxed by its systemd unit (`ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, may write only `/var/lib/dashboard`), and because it runs as root, it validates everything it reads from the config.

## 2. Install smartmontools and check it works

```bash
sudo apt update
sudo apt install smartmontools python3-yaml
sudo smartctl --version | head -1
```

`python3-yaml` is needed by the collector to read `config.yaml`.

Check that `smartctl` can read a disk (you will find the disk IDs in the next step, so come back to this command afterwards):

```bash
sudo smartctl -a /dev/disk/by-id/<disk>
```

You should see the model, serial number, a line like `SMART overall-health self-assessment test result: PASSED`, and (for SATA disks) an attribute table. If this does not work for a disk, the dashboard will not be able to read it either; see the [troubleshooting table](#7-troubleshooting).

## 3. Find your disk IDs

```bash
ls -l /dev/disk/by-id/
```

Example output (shortened, with made-up serials):

```
ata-EXAMPLE_HDD_MODEL_SERIAL0001 -> ../../sda
ata-EXAMPLE_HDD_MODEL_SERIAL0001-part1 -> ../../sda1
ata-EXAMPLE_SSD_MODEL_SERIAL0002 -> ../../sdb
ata-EXAMPLE_SSD_MODEL_SERIAL0002-part1 -> ../../sdb1
nvme-EXAMPLE_NVME_MODEL_SERIAL0003 -> ../../nvme0n1
nvme-EXAMPLE_NVME_MODEL_SERIAL0003_1 -> ../../nvme0n1
nvme-eui.0123456789abcdef -> ../../nvme0n1
scsi-0ATA_EXAMPLE_HDD_MODEL_SERIAL0001 -> ../../sda
usb-EXAMPLE_USB_BRIDGE_SERIAL0004-0:0 -> ../../sdc
wwn-0x5000000000000001 -> ../../sda
```

The same physical disk appears several times under different names. Pick **one** entry per physical disk:

| Use | Do not use |
|---|---|
| The **whole disk**: the name that points at `sda`, `nvme0n1`, ... | Anything ending in `-part1`, `-part2`, ... (partitions) |
| `ata-...` for SATA disks | The `scsi-...` and `wwn-...` entries for a disk you already listed via `ata-...` (duplicates, so the disk would show up twice) |
| `nvme-...` for NVMe disks (use the one **without** a trailing `_1`; both are the same disk) | `nvme-eui.…` when an `nvme-MODEL_SERIAL` entry exists (either works, but pick one and do not list both) |
| `usb-...` for USB disks | `md-...` (that is the RAID device, not a disk) |

Quick way to see only whole-disk entries:

```bash
ls -l /dev/disk/by-id/ | grep -v -- '-part[0-9]' | grep -vE 'wwn-|scsi-|md-'
```

**Why `by-id` and not `sda`, `sdb`...?** Kernel names like `sda` are handed out in the order the disks are detected, so they can change between boots, after adding a disk, or when you move a cable. A by-id name contains the model and serial number and always follows the physical disk. That matters here: a dashboard that shows "sdb is fine" about the wrong disk is worse than none.

## 4. List the disks in `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Edit the `smart.disks` list. One entry per disk:

```yaml
smart:
  stale_after_minutes: 30
  temp_warn_c: 55
  disks:
    - id: ata-EXAMPLE_HDD_MODEL_SERIAL0001
      label: "Data disk 1"
    - id: ata-EXAMPLE_SSD_MODEL_SERIAL0002
      label: "Boot SSD"
    - id: nvme-EXAMPLE_NVME_MODEL_SERIAL0003
      label: "NVMe cache"
```

* `id`: the by-id name from step 3 (no `/dev/disk/by-id/` prefix, no slashes).
* `label`: the friendly name shown in the dashboard (optional).
* `type`: (optional) passes `-d <type>` to smartctl, see below.
* `skip_standby`: (optional, `true`/`false`) passes `-n standby`, see below.

### SATA, NVMe, USB-bridge disks

| Disk | What to set | Notes |
|---|---|---|
| **SATA** (`ata-...`) | nothing | `smartctl` detects it. You get temperature, power-on hours, reallocated/pending/uncorrectable sector counts. |
| **NVMe** (`nvme-...`) | nothing | Detected automatically. You get temperature and power-on hours; "uncorrectable" shows the NVMe *media errors* counter; the reallocated/pending columns show `–` because NVMe has no such attributes. |
| **USB-bridge disk** (`usb-...`) | usually `type: sat` | Many USB enclosures pass SMART through using the SAT protocol, but smartctl cannot always auto-detect it. Test on the command line first: `sudo smartctl -a -d sat /dev/disk/by-id/usb-...`. Some chipsets need a different value, such as `usbjmicron`, `usbsunplus` or `usbcypress`; run `smartctl -d help` for the list. Use the one that works. |
| **USB disk that sleeps** | `skip_standby: true` | Reading SMART wakes a sleeping disk. With this option the collector asks smartctl not to spin the disk up; the dashboard then shows the disk as `standby` instead of keeping it awake all day. |
| **SMART not supported** (many cheap USB bridges, some virtual disks) | see below | |

**When SMART is not supported:** if `smartctl -a` (even with `-d sat`) prints `SMART support is: Unavailable` or `Unknown USB bridge`, nothing can be done in software: the bridge does not forward the SMART commands. The dashboard shows such a disk with status **unknown** and no temperature. Options: take the disk out of the list, plug it in through a different enclosure or a direct SATA port, or accept the "unknown" row.

## 5. Install the script, the service and the timer

From the repository directory, with `config.yaml` filled in:

```bash
sudo bash host/install.sh
```

`install.sh` does exactly this (shown so you know what changes on your system, and so you can do it by hand):

```bash
sudo install -d -m 755 /var/lib/dashboard /etc/dashboard
sudo install -m 644 config.yaml /etc/dashboard/config.yaml
sudo install -m 755 host/dashboard-smart-collect /usr/local/sbin/dashboard-smart-collect
sudo install -m 644 host/dashboard-smart.service host/dashboard-smart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard-smart.timer
sudo systemctl start dashboard-smart.service     # collect once now
```

What goes where:

| File | Installed to | Notes |
|---|---|---|
| `host/dashboard-smart-collect` | `/usr/local/sbin/` | Mode 755, owned by root. It runs as root, so only root may be able to edit it. |
| `host/dashboard-smart.service` | `/etc/systemd/system/` | One-shot job, sandboxed. |
| `host/dashboard-smart.timer` | `/etc/systemd/system/` | Runs 1 min after boot, then every 10 minutes. |
| `config.yaml` | `/etc/dashboard/config.yaml` | A **root-owned copy**. The collector never reads the copy in your home directory, because a root job must not trust a file your own user can change. **After editing `smart.disks`, run `install.sh` again** to refresh the copy. |

**Output file:** `/var/lib/dashboard/smart.json`, owned by root, mode `0644` (world-readable on purpose: it contains model names, serial numbers and health data, but no secrets, and the container's unprivileged user must be able to read it). The directory is mode `0755`.

**How compose mounts it, read-only.** In `docker-compose.yml`:

```yaml
- ${SMART_DIR:-/var/lib/dashboard}:/host/smart:ro
```

The *directory* is mounted (not the single file) because the collector replaces the file atomically (write to `smart.json.tmp`, then rename). A single-file bind mount would keep pointing at the old, deleted file. The `:ro` means the container cannot modify or delete it.

If you changed `SMART_DIR` in `.env`, the collector's output path and the service's `ReadWritePaths=` must be changed to match; the simplest course is to keep the default.

## 6. Verify it

Run the service once by hand and look at the result:

```bash
sudo systemctl start dashboard-smart.service
sudo systemctl status dashboard-smart.service --no-pager      # "Active: inactive (dead)" + status=0/SUCCESS is correct for a one-shot job
ls -l /var/lib/dashboard/smart.json                           # -rw-r--r-- root root, recent time
cat /var/lib/dashboard/smart.json | python3 -m json.tool | head -40
```

For each disk you should see `"passed": true`, a `temp_c` and `power_on_hours`. A disk with a problem has an `"error"` field saying what is wrong.

Check the timer is scheduled:

```bash
systemctl list-timers dashboard-smart.timer
```

`NEXT` should be within about 10 minutes, and `LAST` should fill in after the first run.

Check for errors:

```bash
journalctl -u dashboard-smart.service -n 50 --no-pager
```

Only the start/finish lines are good. Error messages from the collector start with `dashboard-smart-collect:` (for example `no disks listed under smart.disks`, or `smart.disks entry 2: invalid 'type'`).

Then start (or restart) the dashboard and open the **Disk health** section. Within a few seconds you should see your disks with their labels.

**What the dashboard shows when something is wrong.** It never shows old data as if it were fresh:

| Situation | What you see |
|---|---|
| `smart.json` does not exist (collector not installed, not run yet, or wrong mount) | A red box: **"No SMART data: smart.json not found. The host collector ... is not installed, has not run yet, or the compose mount is wrong."** and a warning in the alerts banner. |
| File exists but is unreadable or not valid JSON | A red box saying it cannot be read or parsed, pointing at permissions and `journalctl`. |
| File is **older than `stale_after_minutes`** (default 30; timer stopped, or the job is failing) | A yellow banner above the table: **"⚠ STALE: this data is N old and is NOT current"**, the table is dimmed, and there is a warning in the alerts banner. The footer always shows "Collected N ago". |
| One disk can't be read | That disk's row shows its own error text; the other disks are unaffected. |
| `smart.disks` is empty or invalid | The collector refuses to run and logs the reason (`journalctl -u dashboard-smart.service`); no new file is written, so you get the "not found" box, or the STALE banner if an older file exists. |

To see the stale banner for yourself: `sudo systemctl stop dashboard-smart.timer`, wait for `stale_after_minutes`, and look. (`sudo systemctl start dashboard-smart.timer` afterwards.)

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `smart.json` missing or "No SMART data" box | Timer not installed or never ran, or the compose mount points at a different directory | `systemctl list-timers dashboard-smart.timer`; `sudo systemctl start dashboard-smart.service`; check `SMART_DIR` in `.env`; check `docker compose config` shows `/var/lib/dashboard:/host/smart:ro`. |
| File exists but is empty / `{}` / "no SMART data" persists | Collector crashed before writing, or `smart.disks` is empty | `journalctl -u dashboard-smart.service -n 50`. Typical messages: `python3-yaml is missing` (install it), `no disks listed under smart.disks`, `cannot read /etc/dashboard/config.yaml` (run `install.sh`). The collector never writes a partial file, so an empty file means something else wrote it. |
| `Permission denied` in `journalctl`, or in the container's "cannot be read" box | Collector is not running as root (for example you ran it by hand without `sudo`), or `smart.json` is not world-readable | Run it with `sudo`/systemd. Fix the file: `sudo chmod 644 /var/lib/dashboard/smart.json`; and the directory: `sudo chmod 755 /var/lib/dashboard`. |
| `Smartctl open device: ... failed: Permission denied` (command line) | You ran `smartctl` without `sudo` | Use `sudo smartctl ...`. Only root may open disks. |
| Disk shows **"unknown"**, or an error such as `Unknown USB bridge` | The disk or its USB bridge does not report a SMART verdict | Try `type: sat` (or another `-d` value) in the config and re-run `install.sh`. If `sudo smartctl -a -d sat ...` also says "Unavailable", SMART is not supported through that bridge (see step 4). |
| Disk row shows `not found in /dev/disk/by-id` | Wrong or mistyped `id`, or the disk is gone | Compare with `ls -l /dev/disk/by-id/`; fix `config.yaml`; run `install.sh`. |
| Disk row shows `this looks like a partition` | You listed a `-part1` name | Use the whole-disk name (step 3). |
| Same disk appears twice | You listed both the `ata-` and `wwn-`/`scsi-` names | Keep one entry per disk. |
| Temperature missing (`–`) | Some disks/bridges do not report it; NVMe/SSD/HDD report it differently | Run `sudo smartctl -a ...` and look for `Temperature` or attribute 194/190. If smartctl does not print one, the dashboard cannot either. USB bridges often hide it. |
| Disks behind a **hardware RAID controller** (MegaRAID, PERC, HP Smart Array...) | The OS sees one virtual disk, not the physical drives, so smartctl reads the controller, not the drives | Use the controller passthrough with `type`: `megaraid,N` (N = drive number) or `cciss,N` (HP Smart Array). Give each physical drive its own entry: list the **same `id` of the virtual disk several times**, each with its own `type` and `label` (`type: megaraid,0`, `type: megaraid,1`, ...). Check by hand first: `sudo smartctl -a -d megaraid,0 /dev/disk/by-id/<virtual-disk>`. If the controller can be switched to HBA/IT/JBOD mode, every disk gets its own `ata-` id and no special option is needed (better). This is untested by the author on real hardware RAID controllers. |
| A **USB disk that sleeps** keeps waking up, or reads fail with "Device is in STANDBY" or time out | Reading SMART wakes the disk | Set `skip_standby: true`. The row shows `standby` while it is asleep. The collector runs every 10 minutes, which may be shorter than your disk's idle timer; if the disk never sleeps, raise the timer interval in `/etc/systemd/system/dashboard-smart.timer` (`OnUnitActiveSec=`) and `sudo systemctl daemon-reload`. |
| After changing hardware, a disk shows `not found`, or a new disk is missing | Replaced disk = new serial = new by-id | Re-run `ls -l /dev/disk/by-id/`, update `smart.disks`, run `sudo bash host/install.sh`. |
| I edited `config.yaml` but nothing changed | The collector uses `/etc/dashboard/config.yaml` (the root-owned copy) | Run `sudo bash host/install.sh` again. |
| Dashboard says STALE although the timer is running | The service is failing every run, or the host clock jumped | `systemctl status dashboard-smart.service`; `journalctl -u dashboard-smart.service`; `timedatectl`. |

## 8. RAID (md) panel

The RAID panel is for Linux software RAID managed by `mdadm`. It is read-only and uses two sources:

* `/proc/mdstat` (array summary, rebuild progress and speed), and
* `/sys/block/<md device>/md/` (state, level, degraded flag, sync action, mismatch count and per-member state, slot and error count).

Nothing is written, and nothing runs as root. The dashboard also matches RAID members (`sdb1`) back to the by-id names from the SMART data, so the RAID table shows which physical disk each member is.

**Enable it.** In `.env`, uncomment these two lines (they are commented out by default):

```
COMPOSE_FILE=docker-compose.yml:docker-compose.raid.yml
RAID_DEVICE=md0
```

`docker-compose.raid.yml` adds the two read-only mounts (`/proc/mdstat` and `/sys/block/md0/md`). Then recreate the container: `docker compose up -d --force-recreate dashboard`.

**Find or change the device name.** Run `cat /proc/mdstat`: the first word of each array line (`md0`, `md127`, ...) is its name. Put that name in `RAID_DEVICE` in `.env`. The name must look like `md` plus digits. It is used for the compose mount and the dashboard reads the same value; `raid.device` in `config.yaml` is only the fallback if `RAID_DEVICE` is unset. (An array listed only as `md127` is common after a reinstall.) Only one array is shown; for a second one, you can run a second dashboard instance or open an issue.

**No md RAID?** Leave the two lines commented out in `.env` (or remove the `raid:` block and don't set `RAID_DEVICE`). The dashboard then hides the whole RAID section and raises no alert. If you enabled it by mistake and the array is not there, `docker compose up` fails with a missing mount path error: just comment the lines out again.

If the RAID panel shows `cannot read mdN sysfs`, the mounted path exists but is unreadable; check that `RAID_DEVICE` is the right array, and that the container's user can read `/sys/block/mdN/md/` (normally world-readable).
