# L3 测量表

**English summary:** `l3-measurements.csv` lists what L3 measures on the bench this round: the mechanical
coordinates. Every named arm point in `config/points.yaml` has its own row (left board 12 slots, middle board
14 slots per arm, right board 13 slots, stage and holder points); pick and place share one point. Board slots
also record a nozzle angle (`stage_nozzle_deg` / `holder_nozzle_deg`, a property of the slot): the angle on the
work stage that sets an object picked square from that slot with the nozzle at 0 square on the stage. Tape
gantry paths keep the v1 values (not measured); the tape feed stays an angle. Heater curves, microscope pixel
sizes and arm `min_z_mm` moved to `l3-later.csv` (next round). Measured values go back into `config/` with
`source: measured`, `measured_by`, `date`.

表格：[`l3-measurements.csv`](l3-measurements.csv)（UTF-8，Excel 可直接打开）。这一轮只测**机械坐标**，一共 81 行，
分 Exfoliation 和 Stacking 两块。加热曲线、显微镜像素大小、机械臂最低高度放到下一轮：
[`l3-later.csv`](l3-later.csv)。

## 这一版改了什么（2026-11-13）

- 取和放用**同一个点**：`stage_chip`、`stage_carrier`、`stamp_holder`（v1 里取和放差 1-3 mm，备注里写了 v1 的另一个值）。
- **每个槽位一行**：左板 3 个胶带载玻片槽 + 9 个芯片槽，中间板 14 个（左右臂各一套），右板 5 个印章槽 + 8 个芯片槽。
- **喷嘴角度是槽位的属性**：每个槽位多一列“喷嘴角度”。做法：物体正放在槽里，喷嘴先转到 0 再吸起；送到台子上方，
  转喷嘴直到物体放正，记下角度。以后程序从这个槽取东西时就用这个角度，放回去时反过来。没测的槽位继续用 v1 的算法。
- 喷嘴现在**读不出实际角度**（只能读 x y z），页面显示的是最后一次发出的角度。所以每个机械臂先花 2 分钟查一次规则
  （`nozzle_rule`）：R 是绝对角度还是相对角度、哪边是正。
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
| 喷嘴角度 | 槽位那几行才填：送到台子上放正时的喷嘴角度（印章槽：放上印章架时） |
| 测量人 / 日期 / 备注 | 写回配置时的 `measured_by`、`date`；和 v1 差得多时写看到了什么 |

## 顺序和安全

1. 人站在仪器旁边，一次只动一台。用调试网页（8780）：jog、长按连续移动、或输入目标位置。
2. 开发模式（`limits.yaml` 里 `dev_mode: true`）下没有步长和距离上限，但各轴行程、两臂防撞锁、STOP ALL 还在。
3. 先查喷嘴规则，再测槽位；槽位的角度要在喷嘴先转到 0 的情况下测。
4. 印章接触点：从 z 5.4 往下，看实时画面，第一次出现牛顿环就停。不要靠往下压来测压到多深。
5. 填完后由 Claude 按表改 `config/`，标 `source: measured`，开 PR，并在 `docs/open-questions.md` 里关掉对应的问题。
