# L3 测量表

**English summary:** `l3-measurements.csv` lists what L3 measures on the bench: every named point in
`config/points.yaml`, the unknown limits in `config/limits.yaml` (arm `min_z_mm`, stage x/y travel, stamp
contact z), heater curves, tape feed, turret positions and microscope pixel sizes. Each row has the v1 value
with its `file:line` in the read-only v1 copy, how to measure it, repeats, tolerance, and empty columns for
the measured values. Measured values go back into `config/` with `source: measured`, `measured_by`, `date`.

表格：[`l3-measurements.csv`](l3-measurements.csv)（UTF-8，Excel 可直接打开）。一共 71 行，分 Exfoliation 和 Stacking 两块。

## 每一列

| 列 | 意思 |
| --- | --- |
| 编号 | 和配置里的名字对应，例如 `arm_left.slot_S3` |
| 写回哪里 | 量好后改 qumus-lab 的哪个配置；写“新增”的是现在还没有的项 |
| v1 当前值 / v1 出处 | 从 v1 抄来的值，以及 v1 原码的 `文件:行`（路径相对 `Qumus-lihan-graphene/`） |
| 怎么测 / 次数 / 允许误差 | 建议的做法；每次结果写进“测量 1/2/3” |
| 采用值 | 最后写进配置的数（一般是平均值） |
| 测量人 / 日期 / 备注 | 写回配置时的 `measured_by`、`date`；和 v1 差得多时写看到了什么 |

## 顺序和安全

1. 先做 L1-L2（tape gantry 那几行要等 L2 的问题解决）。
2. 人站在仪器旁边，一次只动一台。用调试网页（8780）：jog、长按连续移动、或输入目标位置。
3. 机械臂往下：`min_z_mm` 量出来之前，每次最多 0.5 mm；印章 z 每次最多 0.1 mm。
4. 印章接触点：从 z 5.4 往下，看实时画面，第一次出现牛顿环就停。不要靠往下压来测压到多深。
5. 加热先只做 30-40 C；更高温度要 Jialei 同意。
6. 填完后由 Codex 或 Claude 按表改 `config/`，标 `source: measured`，开 PR，并在
   `docs/open-questions.md` 里关掉对应的问题。

## v1 里互相矛盾的值（一定要实测）

- 显微镜像素大小：5x 代码用 0.730 um/px、文档写 0.4557；10x 代码 0.365、文档 0.2280；
  100x 代码 0.036、文档 0.0225。
- 画面中心：一处用 (1780, 1180)，另一处用 (1790, 1220)。
- 物镜转盘：v1 只出现 2 = 10x、3 = 20x、4 = 100x；1 = 5x 是推断。
