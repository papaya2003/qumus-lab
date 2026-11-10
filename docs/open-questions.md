# Open questions (only the lab PC or Jialei can answer)

Each item says what the code does now, so nothing waits on the answer. Check them during L1-L2 and
update `config/` (and this page) with what the bench shows.

## Before L1 (ports and identities)

1. **COM ports.** `devices.yaml` uses the v1 numbers (arm left COM7, arm right COM23, supply COM8,
   relays COM4/13/14/15/17/18, reels COM3, gantry COM22, stage COM9, rail COM19, turret COM10,
   focus COM11). Windows may have renumbered them. `probe ports` lists what Windows sees next to the
   config without opening anything.
2. **Kinesis x/y serials.** We use `common/hardware/defaults.py` (x 26007081, y 26007120). A second
   copy in v1 (`agents/processing_agent/workflows/__init__.py`) swaps them. A 0.05 mm jog on each axis
   at L2 will show which is which.
3. **Cameras.** Desk cameras are OpenCV index 0 (left) and 2 (right) as in v1; the microscope camera
   is Uvcham index 0. If OpenCV also sees the microscope camera, the indexes shift. L1 `capture`
   shows which picture comes from which camera.
4. **Thermometer serials.** Exfoliation heater E30559, transfer heater E30753, read through the Dracal
   exe at the path in `devices.yaml`.

## L1 (read only)

5. **Arm on connect.** Opening the port may reset the arm (DTR). We send only `M114`; if the board
   printed a boot banner or reports X0 Y0 Z0, the pose is marked `pose_trusted: false`. v1 never homed
   the arms; we do not home either. Does the arm need homing after power-on?
6. **Relay replies.** We wait up to 0.5 s (`reply_timeout_s`) for a reply to `AT+CH1=1/0` and count anything with `OK`, the
   channel word or the asked state as confirmed. No reply = unknown outcome = module locked. If the
   relays never answer, set `expect_reply: false` per relay (then the switch is "sent, not confirmed").
7. **TP3005P.** v1 writes a literal `/n` (slash n), not a newline, after each command; we copy it
   byte for byte. Does the supply accept it, and does `VOUT1?` answer? (The reading is used only as
   a check.)
8. **Turret.** v1 sent `cRDC<n>\r` and never read the `oRDC` reply; we record it if it comes but do not
   require it (`expect_reply: false`).
9. **Focus position.** TMCL has a position query (GAP), but v1 never used it; our focus position is a
   software estimate from zero at connect, as in v1. Worth adding GAP after L2.

## L2 (one actuator at a time)

10. **Arms have no stop command.** v1 has none. Stopping a moving arm returns `unconfirmed` and locks
    the module. Does the firmware support `M410` or a quick-stop? If yes, add `stop_gcode` in config.
11. **Arm workspace.** `min_z_mm` is unknown for both arms, so real `go_to_point` stays refused (also
    because no point is measured). Measure the safe Z and the slot points at L3.
12. **Stage x/y range.** v1 had no x/y limits for the KZ-100 stage; the rail is 0 to 120 mm. Measure
    the x/y travel before any move larger than the 0.5 mm jog.
13. **Reels.** Degrees per mm of tape are not known; `advance_tape` is capped at 10 deg in real mode.
14. **Gantry.** `M410` (quick stop) is untested on this Marlin. v1's gantry homing sequence is not
    ported; `apply_tape` / `peel_tape` use the v1 relative arcs and need a known start point.
15. **Stamp vacuum** is `safe_state_on`: stop and STOP ALL leave it on so a held stamp does not drop.
    Correct?

## Heaters

16. **First heats.** v1 boosts at 27 V until the target. At low targets (30 to 40 C) that overshoots,
    so in real mode an unmeasured heater only runs `until` at <= 6.5 V and <= 40 C, always with a
    timeout. Measure the curve, then raise the caps in `limits.yaml`.
17. **Transfer heater** has only one v1 hold point (155 C at 16.0 V). Other targets have no hold
    voltage until measured.
18. **Crash while heating.** If the PC or Python dies while the supply is on, nothing in software can
    switch it off until the next start (at connect, a supply that is on with no heater driving it is
    switched off). A hardware timer or thermal fuse on the supply line is still recommended. Closing the
    console window or `taskkill` is a crash in this sense; only Ctrl+C runs safe_state.
19. **Orphan timeout** is 600 s: a heater keeps its own timeout while the agent reconnects, but with no
    lease holder for 10 minutes it is switched off. Is 10 minutes right?

## Software and repo

Found in the 11-08 safety self-review and not changed (see `tests/test_fault_injection.py` for what was):
- `focus_by` sends the TMCL move and returns at once, so a lease expiry during a long focus move does not
  stop it (the 50 um cap keeps it short). Waiting for the move needs the GAP position query (item 9).

20. **Both modules in one process** (shared supply on COM8, Windows COM ports are exclusive). Each
    module still has its own port, lease and lock. OK, or should the supply move to its own service?
21. **`set_switch`, `jog` and `move_axis` are local only** (probe and debug page on the lab PC), with switches
    turning off after <= 10 s. The agent will need L3 actions that hold vacuum for minutes; they come
    after the bench is measured.
22. **Middle-board lock** covers the slot points S1-S14 of both arms (`config/points.yaml` `zones:`).
    Are there other places both arms reach (the hand-over route, the stage edges)? Restart
    reconciliation of `qumus.db` and the camera freshness limit (10 min proposed) are still open.
23. **Default branch.** The branch was pushed before `main` existed, so GitHub may show it as the
    default. Set the default branch to `main` in the repo settings.
24. **Fonts.** The debug page loads Barlow from Google Fonts; offline it falls back to Segoe UI.
25. **Bad parameters** return `400` (shared ADR) rather than `422` (the brief).
