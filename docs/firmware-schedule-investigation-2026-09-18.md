# Firmware and schedule investigation, 18 September 2026

## Conclusion

The battery supplied the overnight EV/house load instead of receiving its intended scheduled charge. The API history confirms this independently of the screenshots.

There is a confirmed compatibility gap in the scheduler: it only writes the legacy three-slot schedule (CID 103), while the inverter returns the capability marker that Solis's command workbook says requires the newer six-slot parameters. The legacy charge window is still present, but the new slots are disabled. Firmware migration/reset is a strong explanation; the precise overnight sequence is not proven without the Pi's execution log or Solis control history.

The owner reports that the firmware was updated on 17 September, Feed-in Priority was selected overnight, and Solis AI was **not enabled**. Self-Use was selected manually on the morning of 18 September to charge from solar. Therefore today's Self-Use readback must not be treated as evidence of last night's operating mode, and AI must not be blamed merely because it became available.

Initial investigation scope: authenticated read-only calls using `.env`, local source/document review, official documentation lookup, and mocked scheduler execution. During that initial phase, no `/v2/api/control` calls, Discord messages, clock changes, mode changes, or schedule writes were performed, and the scripts were unchanged. The subsequent requested implementation and live repair are documented at the end of this report.

## Overnight evidence

Source: `/v1/api/inverterDay`, queried for 2026-09-18. Times below are `dataTimestamp` converted to `Europe/Dublin` (UTC+1 on this date). The endpoint returned misleading `time` strings seven hours ahead of these local times despite `timeZone: 1`; those strings were not used for the timeline. Timestamp conversion agrees with the owner's screenshots and the current telemetry timestamp.

| Irish local time | Battery SOC | Battery power | House/EV load | Interpretation |
| --- | ---: | ---: | ---: | --- |
| 02:02:25 | 61% | -0.44 kW | 0.34 kW | Modest discharge before the large load |
| 02:07:25 | 59% | -5.20 kW | 7.63 kW | Battery is supplying much of the load |
| 02:42:24 | 45% | -5.23 kW | 7.66 kW | Continued substantial discharge |
| 03:47:25 | 22% | -4.25 kW | 7.65 kW | Battery heavily depleted |
| 03:52:25 | 12% | 0 kW | 7.58 kW | Abrupt SOC drop in telemetry |
| 03:57:28 | 11% | +4.50 kW | 7.57 kW | Brief battery charging |
| 04:12:25 | 16% | +4.58 kW | 7.84 kW | Brief charging continues |
| 04:17:24 | 16% | -4.40 kW | 7.93 kW | Short return to discharging |
| 04:22:25 | 15% | 0 kW | 7.93 kW | Battery becomes idle |
| 05:57:25 | 15% | 0 kW | 2.52 kW | No sustained overnight refill |

Negative battery power in this response is discharge, as independently confirmed by falling SOC. Positive battery power is charging. The load attribution to the EV comes from the owner, rather than appliance-level metering in this API.

The short recharge near 04:00 is consistent with low-SOC recovery. That remains an inference: the history does not identify which controller triggered it. Today's telemetry reports overdischarge SOC 15% and force-charge SOC 10%. The historical SOC has discontinuities, including one isolated 0% sample at 04:37 followed by 15%; do not interpret that as verified physical depletion to zero.

## Live readback after the morning mode change

Readbacks collected approximately 14:18-14:26 Irish local time. The settings are today's snapshot, not an archived overnight configuration.

| Parameter | Readback | Significance |
| --- | --- | --- |
| Firmware | `version=510052`, `version2=030000`, `hmiVersionAll=5103`, `dspmVersionAll=5200` | Current reported versions; no pre-update snapshot |
| CID 6798 | `43605` = `0xAA55` | Workbook directs use of newer scheduling parameters |
| CID 103 | Charge 1: `100 A, 02:05-05:55`; discharge cleared; other legacy slots zero | Old schedule still exists |
| CIDs 5916-5921 | All `0` | All six new charge slots disabled |
| CIDs 5922-5927 | All `0` | All six new discharge slots disabled |
| CID 5946 | `00:00-00:00` | New charge slot 1 time |
| CID 5948 / 5928 | `50` / `50` | New charge slot 1 current / SOC |
| CID 5964 | `00:00-00:00` | New discharge slot 1 time |
| CID 5967 / 5965 | `50` / `50` | New discharge slot 1 current / SOC |
| CID 636 | `33` (`0x21`) | Self-Use and grid-charge permission bits set, consistent with morning intervention |
| CID 543 | `msg=0`, `yuanzhi=33` | Legacy global Time-of-Use bit off; subsequent live validation established that this is not an enable requirement for the new six-slot firmware |
| CID 56 | `2026-09-18 14:20:49` | About four seconds behind API response time converted to Dublin; no present one-hour clock error |

The command workbook identifies the new slot current and SOC fields, but omits detailed write formats/scaling for several new parameters. The read values above are the API's decoded values. No write compatibility was tested.

## Findings in the script

1. **No new schedule support.** `set_inverter_parameters()` and `clear_discharge_slots()` always write CID 103. Neither detects CID 6798 nor writes the new per-slot enable, time, current, and SOC fields. Clearing legacy discharge slots does not establish that the new discharge slots are cleared. Writing a legacy charge window does not establish that a new charge slot is enabled.
2. **No enable or grid-charge/mode verification.** The script assumes the required operating settings already exist. New firmware requires the per-slot switches; the legacy global Time-of-Use bit is not the correct enable test on the updated inverter. A repair should preserve the owner's selected Feed-in Priority policy rather than silently choose Self-Use.
3. **No verified success after writes.** The script logs HTTP/API responses but does not reject nonzero API/device result codes, handle asynchronous completion comprehensively, or read back and compare the active schedule. A normal process exit is not evidence that the intended schedule was applied.
4. **The 20% target is only arithmetic.** `DISCHARGE_SOC=20` reduces the calculated current; it is not written as a hardware discharge cutoff. Actual capacity, house load, current limits and SOC estimation can prevent the result from ending at 20%. Today's hardware overdischarge limit reads 15%.
5. **Low SOC leaves existing schedules untouched.** At SOC <=20%, `manage_discharge()` returns without clearing a previous discharge setting or ensuring overnight charging. Mocked calls with SOC 20 and 15 confirmed zero writes.
6. **The hardcoded charge window is 02:05-05:55.** `--clear` reconstructs this window at 100 A; it does not preserve whatever charge settings are actually on the inverter. Confirm this intended window against the current tariff before implementing a replacement.
7. **`.env` is not loaded by either existing script.** They expect exported environment variables. The investigation explicitly loaded `.env` into its own process before importing the signing helpers. This is not established as an overnight problem because the Pi may already export its environment.
8. **Other validation gaps.** `--hours` accepts zero/negative or excessive durations; `--sync-time` uses the host's local timezone without enforcing Dublin. Neither was exercised against the inverter.

The Hybrid Inverter sheet's CID 103 example uses separate comma-delimited start/end times, whereas the old script uses hyphenated ranges. The workbook's **Demo sheet also explicitly uses the hyphenated format**, agreeing with live CID 103 readback. There is no basis here to blame the original hyphenated time format. Delimiter-only changes would not fix the missing six-slot configuration.

Offline verification replaced SOC reads, control writes, and Discord notification functions with mocks. SOC 80% produced CID 103 discharge windows 00:00-02:00 for `--hours 2` and 01:00-02:00 for `--hours 1`, both with the charge window 02:05-05:55. These checks did not send commands.

## API and documentation status

- Existing HMAC credentials and the script's signing helper successfully authenticated against `inverterDetail`, `inverterDay`, and `atRead`. There is no evidence of an authentication or read-endpoint break. Write behavior remains untested.
- The bundled **SolisCloud Platform API Document V2.0.3**, revision history page 2, is dated 24 December 2025. It covers telemetry/history, not the full current control contract.
- The bundled command workbook, **Hybrid Inverter row 268**, already says to use the new parameters when CID 6798 returns `0xAA55`. Rows 268-327 enumerate the six charge and six discharge slots. This is not a newly discovered undocumented register scheme.
- The public official command workbook downloaded during this investigation had HTTP Last-Modified **6 June 2025**. The relevant legacy and six-slot command IDs/names agree with the local workbook. Formatting/content differences do not establish a newer firmware-specific contract.
- Solis's current developer FAQ states that CID-based control is being phased out in favor of `modbusAddr`. The user-auth control guide still documents HMAC and the `:13333` base URL. Its full hybrid register protocol must be obtained from Solis. CID numbers must not be assumed to equal Modbus register addresses.
- Current API release notes list a **26 August 2026** addition of `hmiVersionAll`. They do not establish a control API change on 17 September or describe this inverter's firmware migration behavior.
- Some reads timed out. Sequential read retries obtained the relevant settings. Successful `atRead` responses often had `needLoop=false` and carried the value in `data.msg`; their `orderId` sometimes repeated that value. A follow-up `/result` query returned `data=null`, which must not overwrite a completed read result.

Official references, checked 18 September 2026:

- [Current user-auth device control API](https://developer.soliscloud.com/guide/device-control-v1.html)
- [API FAQ, including CID deprecation](https://developer.soliscloud.com/guide/faq.html)
- [API release notes](https://developer.soliscloud.com/guide/release-notes.html)
- [Official command workbook](https://oss.soliscloud.com/doc/SolisCloud_control_api_command_list.xlsx)
- [Separate device-control PDF](https://oss.soliscloud.com/doc/SolisCloud%20Device%20Control%20API%20V2.0.pdf)
- [Solis remote control settings guide](https://solis-service.solisinverters.com/en/support/solutions/articles/44002638862-solis-cloud-remote-control-settings-desktop-version)
- [Solis AI and other operating modes](https://solis-service.solisinverters.com/en/support/solutions/articles/44002688537-inverter-control-solis-ai-more)

## Proposed repair and unresolved attribution

The next implementation should detect the schedule capability, explicitly configure the applicable slots and their enable/SOC/current fields, verify grid-charging permission and (for legacy firmware only) Time of Use while preserving unrelated mode bits, and read back every changed value. A failed or ambiguous write should cause a failed run, rather than a success-looking notification. Do not assume a legacy `--clear` has restored the active charge schedule.

Before a live write, snapshot all affected settings, confirm the intended charge window/current/SOC and Feed-in Priority policy, and verify the new command encodings or obtain the matching hybrid Modbus protocol from Solis. Any AI/remote-dispatch ownership check should be observational; the owner says AI was never enabled.

To establish the exact overnight cause, obtain the Pi's `solarSet.log` and rotated logs, execution timezone/schedule, and Solis control/firmware history spanning the update and night. Current readbacks were collected after a manual mode change. They prove a present mismatch and a script capability gap, but cannot distinguish a firmware reset, migration behavior, an unconfirmed/rejected script write, or another settings change as the sole trigger.

Redacted API evidence and the selected history fields are retained in `diagnostics/2026-09-18-readback.json`. The initial firmware/legacy-schedule/marker/clock findings were captured in the investigation console and are summarized above; the JSON contains the later captured slot/mode/history responses.

## Subsequent implementation

The owner subsequently selected Feed-in Priority again and requested the scheduler
repair. The historical findings above describe the original script and remain an
investigation snapshot. The updated standalone `script.py` implements
capability detection, six-slot control, current/SOC/time/enable verification,
preserved operating-mode bits, low-SOC charge recovery, and settings snapshots.
See the README for commands and deployment requirements. Local tests are separate
from live control validation and the following night's actual operation.

During live validation, the new slot time/current/SOC/enable writes succeeded.
The first pass stopped on a read timeout. Recovery then exposed an incorrect
assumption in the initial diagnosis: requesting the legacy timed mode value 98
was acknowledged but repeatedly read back as 96. The maintained solis-sensor
`ALL_CONTROLS[True]` mode table uses 96 for Feed-in Priority/grid charging and
omits timed variants; `ALL_CONTROLS[False]` retains 98 for legacy firmware.
The corrected implementation requires the individual slot enable switches on
new firmware. The owner subsequently requested new-firmware-only support, so
the legacy CID 103 fallback and global Time-of-Use bit writes were removed.
Any capability marker other than `0xAA55` now stops scheduling without writes.
No dedicated legacy CID 543 write was attempted. Feed-in Priority remained 96.

### Final validation

All 25 offline tests passed after correcting the firmware-specific mode handling.
Final live read-only verification passed for every managed setting:

- Capability marker 43605 (`0xAA55`): six-slot firmware.
- Charge slot 1 enabled; 02:05-05:55; 100 A; 100% SOC target.
- Charge slots 2-6 disabled.
- All six discharge slots disabled. Discharge slot 1 is cleared to 00:00-00:00,
  0 A, with its future discharge cutoff set to 20% SOC.
- Mode 96: Feed-in Priority with grid charging allowed, using the new slot enables.

The pre-write backup is `snapshots/20260918-145558-652258.json` (Git-ignored).
The final redacted verification is `diagnostics/2026-09-18-repair-verification.json`.
The initial timeout and ignored legacy mode-bit attempt were not counted as
successful runs; the final evidence is the complete read-only verification using
the corrected firmware contract. No Discord notifications were sent.

Actual overnight charging and an enabled discharge cycle have not yet been
observed with the repaired setup. The local code is updated; the Pi deployment
remains pending its SSH host/path or an owner-managed file update. The owner
clarified that the Pi runs one file with hardcoded credentials. The API client
was therefore folded into `script.py`; only that file needs copying, retaining
the Pi's configuration values at the top. `.env` remains an optional local
fallback. Dependencies are listed in `requirements.txt`.
