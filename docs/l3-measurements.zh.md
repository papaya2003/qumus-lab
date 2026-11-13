# L3 测量表

**English summary:** `l3-measurements.csv` lists what L3 measures on the bench this round: the mechanical
coordinates. Every named arm point in `config/points.yaml` has its own row (left board 12 slots, middle board
14 slots per arm, right board 13 slots, stage and holder points); pick and place share one point. Board slots
also record a nozzle angle (`stage_nozzle_deg` / `holder_nozzle_deg`, a property of the slot): how many degrees
to turn the nozzle after picking from that slot so the object sits square on the work stage. The nozzle turns BY
an angle (v1 `M2101 R` is relative) and cannot be read back, so the value is the debug page's "since pick" total;
going back to the slot turns the same amount the other way. Tape
gantry paths keep the v1 values (not measured); the tape feed stays an angle. Heater curves, microscope pixel
sizes and arm `min_z_mm` moved to `l3-later.csv` (next round). Measured values go back into `config/` with
`source: measured`, `measured_by`, `date`.

表格：[`l3-measurements.csv`](l3-measurements.csv)（UTF-8，Excel 可直接打开）。这一轮只测**机械坐标**，一共 81 行，
分 Exfoliation 和 Stacking 两块。加热曲线、显微镜像素大小、机械臂最低高度放到下一轮：
[`l3-later.csv`](l3-later.csv)。

## 这一版改了什么（2026-11-13）

- 取和放用**同一个点**：`stage_chip`、`stage_carrier`、`stamp_holder`（v1 里取和放差 1-3 mm，备注里写了 v1 的另一个值）。
- **每个槽位一行**：左板 3 个胶带载玻片槽 + 9 个芯片槽，中间板 14 个（左右臂各一套），右板 5 个印章槽 + 8 个芯片槽。
- **喷嘴角度是槽位的属性，意思是“转多少度”**：喷嘴每次是在当前位置上再转一个角度（v1 的 `M2101 R` 就是这样），
  而且读不出现在指向哪里。所以槽位角度 = 从这个槽吸起物体后，要再转多少度，放到台子上才是正的。
  做法：从槽里吸起物体，送到台子上方，在调试页 nozzle 框里一点点转（比如 +10、-2，按 Turn），直到物体放正；
  页面上显示的 since pick（这次拿起后一共转了多少度）就是这个槽位的角度。以后程序从这个槽取东西时转 +角度，
  放回这个槽时转 -角度。没测的槽位继续用 v1 的做法（拿上去转 0，放回去用 v1 的公式）。
- `nozzle_rule` 只是顺手记一下 + 是顺时针还是逆时针，可以跳过。
- 贴胶带、揭胶带的路线沿用 v1，不测。卷轴只用角度：在胶带卷最小半径时，换一段新胶带要转多少度。
- 不再测 `min_z`。

## 每一列

| 列 | 意思 |
| --- | --- |
| 编号 | 和配置里的名字对应，例如 `arm_left.slot_S3` |
| 写回哪里 | 量好后改 qumus-lab 的哪个配置；写“新增”的是现在还没有的项 |
| v1 当前值 / v1 出处 | 从 v1 抄来的值（左右板没有 v1 固定值，写的是草图估计值），以及 v1 原码的 `文件:行` |
| 怎么测 / 次数 / 允许误差 | 建议的做法；每次结果写进“测量 1/2/3”（xyz 写成 `X.. Y.. Z..`） |
| 采用值 | 最后写进配置的数（一般是平均值） |
| 喷嘴角度 | 槽位那几行才填：从这个槽吸起后要转多少度才放正（调试页 since pick 的数；印章槽：送到印章架） |
| 测量人 / 日期 / 备注 | 写回配置时的 `measured_by`、`date`；和 v1 差得多时写看到了什么 |

## 顺序和安全

1. 人站在仪器旁边，一次只动一台。用调试网页（8780）：jog、长按连续移动、或输入目标位置。
2. 开发模式（`limits.yaml` 里 `dev_mode: true`）下没有步长和距离上限，但各轴行程、两臂防撞锁、STOP ALL 还在。
3. 量槽位角度时，每个槽位都重新吸起一次（吸起时 since pick 自动归零），转好后直接抄 since pick。
4. 印章接触点：从 z 5.4 往下，看实时画面，第一次出现牛顿环就停。不要靠往下压来测压到多深。
5. 填完后由 Claude 按表改 `config/`，标 `source: measured`，开 PR，并在 `docs/open-questions.md` 里关掉对应的问题。
