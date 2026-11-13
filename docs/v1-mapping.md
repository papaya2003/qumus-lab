# v1 mapping and hazards

v1 = `Qumus-lihan-graphene` (read only). From v1 we took the bytes each instrument receives and the
measured numbers. We did not take its structure: v1 mixed interfaces and drivers and had one function
per place (`home_to_exfoliation_stage`, `place_silicon_chip_on_exfoliation_stage`, ...). Here a driver
has a few generic operations, places are named points in `config/points.yaml`, and sequences are
composed by module actions.

`tests/test_bytes_vs_v1.py` runs 17 operations through the unchanged v1 class and through the new
driver on the same emulator and compares the bytes on the wire. The v1 bytes are also frozen in
`tests/golden/v1_bytes.json`, so the comparison runs on the lab PC without the v1 tree.

## v1 class to new driver

| v1 (common/hardware/...) | New driver | Generic operations | Byte test |
| --- | --- | --- | --- |
| `robot_arm_controller.RobotArmController` | `drivers/gcode_arm.py` `GcodeArm` | `move_to(x, y, z, feed)`, `read`, `set_vacuum`, `release_item`, `rotate_nozzle`, `dwell`, `finish_moves` | arm_move_to, arm_park_left, arm_pick, arm_place, arm_rotate_nozzle, arm_vacuum_on_off |
| `relay_controller.RelayController` | `drivers/relay.py` `RelaySwitch` | `set(on)` | relay_on_off |
| `power_controller.TP3005PController` | `drivers/power_supply.py` `TP3005P` | `set_voltage`, `output`, `read` | psu_heat_sequence |
| `temp_controller` (Dracal CLI) | `drivers/thermometer.py` `DracalThermometer` | `read` | dracal command line |
| `transfer_control_controller` + exfoliation heat flow | `drivers/heater.py` `Heater` | `heat(target_c, timeout_s, mode)`, `off` | (uses relay + PSU bytes above) |
| `motor_controller` (tape reels Arduino) | `drivers/tape_reels.py` `TapeReels` | `power(motor, on)`, `rotate(motor, cw, deg)` | reels_release_new_tape, reels_adjust_negative |
| `printer_controller` (Marlin) | `drivers/printer_gantry.py` `PrinterGantry` | `line`, `arc_relative` (100 segments as v1), `power`, `finish`, `stop` | gantry_apply_tape, gantry_peel_tape, gantry_power |
| `sample_PDV_controller.SamplePDVController` (KZ-100) | `drivers/kz100.py` `KZ100Link`, `KZ100Stage` | `move_to`, `move_by`, `read`, `stop`, `set_max_speed` | stage_move_axis_to, stage_read_stop |
| `focus_controller.FocusController` (TMCL) | `drivers/focus_tmcl.py` `TmclFocus` | `move_by_um`, `stop` | focus_moves_and_stop |
| `turret_controller.TurretController` | `drivers/turret.py` `Turret` | `rotate_to(n)` | turret_rotate |
| `KDC101_controller`, `KST201_controller`, `stamp_PDV_controller` | `drivers/kinesis.py` `KinesisAxis`, `StampManipulator` | `move_to`, `move_by`, `read`, `stop` | (.NET calls, no serial bytes) |
| `camera_controller`, `stamp_video_recorder` | `drivers/cameras.py` `Camera` | `capture` | |

v1 place functions became points, for example `RobotArmController.home_to_exfoliation_stage()` is
`go_to_point arm_left.park` (`move_to(0, 220, 95, feed=5000)`, same bytes), and the slot positions of
the middle board are `arm_left.slot_S1..S14` / `arm_right.slot_S1..S14`.

## Hazards not copied

| v1 behaviour | Risk | New behaviour | Test |
| --- | --- | --- | --- |
| Arm `connect` sent `M17` (motors on) and the reels powered on at connect | opening a port could energise motors | connect only queries (`M114`); `G21`/`G90` are sent once before the first move | test_arm_connect_v1_powers_motors_new_only_queries, test_reels_and_printer_connect_send_nothing_but_queries, test_arm_first_move_sends_units_and_absolute_once |
| Gantry v1 enabled motors at connect (`M17 X Y Z`) and paused 50 ms after each acknowledgement | omitting motor enable on a jog can leave the gantry silent; an unread position-query acknowledgement can be mistaken for the next command's reply | enable motors before the first explicit motion, consume the full `M114` reply through `ok`, and retain v1's 50 ms command pause; connect remains query-only | test_first_move_enables_motors_but_connect_only_queries, test_position_query_consumes_its_delayed_acknowledgement |
| Default arm feed `F5000` when none given | fast moves by accident | feed is required, and capped by `limits.yaml` | test_arm_move_needs_a_feed |
| Heaters had no timeout; 27 V boost until target | a crash or hang leaves a heater on | mandatory `timeout_s`, overshoot cut-off, 3 failed reads switch off, orphan timeout 600 s without a lease holder | test_heat_runs_with_timeout_and_heat_off_any_client |
| KZ-100 port opened and closed per command | lost echo characters, slow | one persistent port, same bytes | test_kz100_link_keeps_port_open_same_bytes |
| Kinesis `EnableDevice` at connect and blocking `MoveTo(pos, 60000)` | motors energised at start, no stop during a move | enable on the first move; `MoveTo(pos, 0)` then poll, so `stop` (`StopImmediate`) works | test_kinesis_enables_on_first_move_and_moves_without_blocking |
| Focus `move_to_um` computes `position - target` (opposite sign to `move_relative_um`) | focus moves the wrong way | only relative moves (`move_by_um`) are ported; moves over 30 000 um are refused | test_focus_refuses_moves_over_30_mm |
| No stop for the arms | a moving arm can not be stopped | `stop` on a moving arm returns `unconfirmed` and locks the module until a person checks it | test_stopping_a_moving_arm_is_unconfirmed_and_locks |
| One function per place with coordinates in code | coordinates change in many places | one `move_to`; coordinates in `points.yaml` with `source: v1` and the cited line; real motion only to `source: measured` points | test_config_cites |
