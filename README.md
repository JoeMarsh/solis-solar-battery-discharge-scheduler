# Solis solar battery discharge scheduler

Configures overnight charging and calculates pre-charge discharge current from
battery SOC. Uses only the new six-slot schedule, requiring capability marker
CID 6798 = `0xAA55`. There is no legacy CID 103 write path or fallback.
Unsupported or unreadable capability markers stop the run without writing.

## Setup

Requires Python 3.9+:

```sh
python -m pip install -r requirements.txt
```

The Pi deployment is a **single file: `script.py`**. Copy your existing hardcoded
`API_KEY`, `API_SECRET`, `INVERTER_SN`, and optional `DISCORD_WEBHOOK_URL` into the
configuration fields at the top before replacing the Pi's script. Those populated
fields take precedence. No `.env` file or second Python file is required.

For local use, fields left as placeholders can fall back to environment variables
or the optional `.env` beside the script. Exported environment variables take
precedence over `.env`. The loader accepts simple assignments, not shell commands
or variable expansion. The API host remains `https://www.soliscloud.com:13333`.

## Commands

```sh
# Read the actual inverter and preview changes. No writes or notifications.
python script.py --clear --dry-run

# Restore overnight charging and disable all scheduled discharge.
python script.py --clear

# Recalculate discharge ending at 02:00, while also configuring charging.
python script.py --hours 2
python script.py --hours 1

# Read the six-slot schedule, or selected individual parameters.
python read_inverter_schedule.py
python read_inverter_schedule.py --cid 636 --cid 5946 --cid 5916

# Explicit clock correction using Irish local time, including DST.
python script.py --sync-time
```

`--no-notify` suppresses Discord messages. `--env-file PATH` selects another env
file. Existing Pi invocations using `--hours`, `--clear`, or `--sync-time` remain
available. Copy **only `script.py`**, retaining your Pi's hardcoded configuration.
The Python environment needs `requests` and timezone data (`tzdata` if system
timezone data is unavailable); `requirements.txt` lists these dependencies.
Updating this checkout alone does not deploy to the Pi.

## Managed settings

The existing policy is retained: charge **02:05-05:55 at 100 A**, target **100% SOC**;
discharge ends **02:00**, with current calculated from **400 Ah** nominal capacity
and a **20% SOC** target, capped at **100 A**. These values are constants near the
top of `script.py`; confirm them against your current installation and tariff.
`--hours` accepts 1-20 whole hours, avoiding overlap with the charging period.

The script owns the six charge/discharge slot enable
settings. It uses slot 1, disables unused slots, and sets explicit current and SOC
limits. Times/current/SOC in disabled slots 2-6 are left untouched. Voltage-based
slot parameters and global battery protection settings are not modified.

The script preserves the selected **Feed-in Priority or Self-Use** mode and other
mode bits, while enabling **grid charging**. Six-slot firmware uses individual
slot enable switches and does not set the old global **Time of Use** bit.
On the tested HMI 5103, requesting mode 98 reads back as 96; mode 96 is the
correct new-firmware Feed-in Priority/grid-charge mode and must not be treated as
failed scheduling. The script rejects other base modes. Keep automatic AI/third-party scheduling off when this script owns the
schedule. It does not enable or disable Solis AI itself.

At SOC <=20%, it **still restores charging and disables discharge**. `--clear`
does the same without needing an SOC query. It restores the configured charging
window, rather than preserving arbitrary charge settings from the app.

The 20% discharge target is used both for current calculation and the actual
per-slot SOC cutoff.

## Verification and recovery

Before writing, the script reads all managed settings and saves a JSON snapshot
under `snapshots/`. Writes include a freshly read `yuanzhi` value so switching one
bit does not overwrite sibling flags. Affected slots are disabled while their
parameters change. Charging is configured before discharge is enabled.

Every changed setting is read back, followed by a final check of the whole managed
configuration. HTTP errors, Solis/device errors, incomplete reads, and mismatches
produce a nonzero exit status. Read-only network failures have a bounded retry;
ambiguous writes are not automatically repeated. After a six-slot update failure,
the script attempts to leave discharge slot 1 disabled, retaining any successfully
restored charge configuration. This is not an atomic device transaction: inspect
the log and current settings after any failed run. Old snapshots are not blindly
restored across firmware versions.

Logs are in `solarSet.log`, rotated at 1 MB with four backups. Snapshots, logs,
credentials and Python caches are ignored by Git. Notifications report success
only after verification. The script does not change your cron/systemd schedule.

```sh
python -m unittest discover -s tests -v
```

Tests emulate shared switch registers and failed/mismatched writes without
contacting SolisCloud or Discord. Live readback verifies configuration; actual
overnight charging remains a separate operational check.

## Protocol references

- Bundled `docs/SolisCloud_control_api_command_list.xlsx`: Hybrid Inverter rows
  268-327 define the new slots and capability marker; Demo and Switch list per Bit
  describe Time of Use and bit-preserving control.
- [Official control API](https://developer.soliscloud.com/guide/device-control-v1.html)
  and [CID deprecation notice](https://developer.soliscloud.com/guide/faq.html).
  This repair uses the new slot CIDs verified as readable on this inverter. It
  does not guess Modbus addresses from CID numbers.
- [Solis integration control definitions](https://github.com/hultenvp/solis-sensor/blob/master/custom_components/solis/control_const.py)
  corroborate per-slot time, current and SOC formats. New time slots read as
  `HH:MM-HH:MM`; control submissions use `HH:MM,HH:MM`. The client handles this
  conversion. Currents are amperes and SOC values are percentages. The new-firmware
  mode table uses 96 for Feed-in Priority with grid charging.
- [Investigation and captured evidence](docs/firmware-schedule-investigation-2026-09-18.md).
