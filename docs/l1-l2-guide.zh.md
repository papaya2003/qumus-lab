# 实验室 PC 上手指南：安装、自检、L1、L2

给 Jialei 在实验室 PC 上一步一步照着做。每一步写了 **看什么** 和 **怎样算通过**。
命令都在 `cmd`（命令提示符）里输入，先进入仓库文件夹，例如：

```bat
cd C:\Users\Qumus_PC\qumus-lab
```

> 安全规则：L1 只读，什么都不会动。L2 每次只动一个仪器，每步动之前都要按 Enter。
> 任何时候按 **Ctrl+C**：所有轴停下、两个加热台断电。真出事就直接关仪器电源。

---

## 第 0 步：准备

1. 实验室 PC 装好 Python 3.13（python.org 版本，安装时勾选 "Add python.exe to PATH"）。
2. 把 `papaya2003/qumus-lab` 仓库的 main 分支下载到上面的文件夹。

## 第 1 步：安装（只需一次）

```bat
scripts\setup.bat
```

- 看什么：最后一行。
- 通过：显示 `Done. Next: scripts\check.bat`，中间没有 `ERROR`。

## 第 2 步：自检

```bat
scripts\check.bat
```

它检查 Python、安装的包、配置文件、厂商软件路径（Dracal、Kinesis、Uvcham），列出 Windows 能看到的
COM 口，并在内存里用模拟仪器把整套程序和调试网页启动一次。它不打开任何仪器端口。

- 看什么：每行开头的 `[PASS]` / `[WARN]` / `[FAIL]`，以及最后一行。
- 通过：最后一行是 `0 FAIL`。
- `[WARN]` 可以先继续，但要读一下：比如某个 COM 口没看到（仪器没开电源或 USB 没插），或某个厂商
  软件文件夹不在配置写的位置。把 WARN 那几行拍照或复制给 Claude。
- `[FAIL]` 必须先解决（复制给 Claude）。

## 第 3 步：关掉 v1

一个 COM 口同一时间只能被一个程序占用。

1. 关掉所有 Qumus v1 窗口和 v1 的 Python 程序（任务管理器里不要有 v1 的 `python.exe`）。
2. 打开所有仪器的电源。

- 通过：第 4 步里没有 "端口被占用"（`PermissionError` / `Access is denied`）的报错。

## 第 4 步：L1 端口对照（不打开端口）

```bat
scripts\probe.bat ports
```

- 看什么：每个仪器一行，配置里的 COM 口后面写 `present` 或 `MISSING`。最后几行 `(not in config)`
  是 Windows 看到、但配置里没写的口。
- 通过：所有行都是 `present`。
- 有 `MISSING`：打开 Windows「设备管理器 → 端口 (COM 和 LPT)」，看这台仪器现在是几号口，告诉 Claude
  （或直接改 `config\devices.yaml` 里那一行的 `port:`）。

## 第 5 步：L1 全部读一遍（只读，不动）

```bat
scripts\probe.bat l1
```

程序依次连接每个仪器，只发查询命令，然后每个相机拍一张照片。什么都不应该动、不应该咔哒响、不应该加热。

- 看什么：每个仪器下面一行 `[OK] read` 加读数；最后 `L1 read: 18/18 devices answered`；
  每个相机一行 `picture: ...`（照片文件路径）。
- 通过（逐项）：

| 仪器 | 怎样算通过 |
| --- | --- |
| 两个机械臂 `arm_left` / `arm_right` | 有 x / y / z 数字；`pose_trusted` 为 `false` 说明刚上电或复位过，记下即可 |
| 继电器（真空、风扇） | `confirmed: true`。如果是 `false` 且 `raw_reply: null`，说明继电器不回话，告诉 Claude |
| 两个加热台 | `temp_c` 是室温（大约 20–25 °C），`phase: off` |
| 电源 `power_supply` | `[OK]`，电压接近 0 |
| 样品台、导轨、焦距、转盘、印章 | `[OK]` 有读数 |
| 三个相机 | 打开照片：左桌面相机拍到左侧，右桌面相机拍到右侧，显微镜相机是显微画面。对不上就告诉 Claude |

- 报告保存在 `runs\real-lab\probe\` 里（`...-l1-report.json`）。
- 读到的值也写进了状态表：`scripts\state.bat devices` 可以看到每台仪器最后一次读数和读取时间。

## 第 6 步：L2 一次只动一个仪器

开始前：清空机械臂、龙门、印章下方的东西（不要放芯片、印章），手放在仪器电源开关附近。

```bat
scripts\probe.bat l2
```

程序一共 22 步，先左边（exfoliation，10 步）后右边（stacking，12 步）。每个模块按相同顺序：相机 → 开关 → 运动（每次运动后立即返回起点）→ 加热台最后。屏幕上会显示 `[exfoliation 3/10]` 这样的步骤编号。每一步：

1. 程序先说要做什么，然后问 `[Enter = do it, s = skip, q = quit]`：按 Enter 执行，`s` 跳过该仪器及其后续步骤，`q` 结束。
2. 做完后回答：`y` = 实际动作正确；`n` = 有问题，但仪器已静止且安全，记录问题并跳过该仪器剩余步骤；`q` = 结束。
   跳过会包括返回动作，不会假定仪器已回到起点。继续前必须确认有问题的仪器不会影响其他设备。

| 步骤 | 你应该看到 / 听到（= 通过） |
| --- | --- |
| 左桌面相机拍照 | Windows 会自动打开保存的照片；观察后回到终端回答 |
| 剥离真空 on、风扇 on | 泵或风扇启动，**10 秒内自己关掉** |
| 左机械臂 z +10 mm，再 -10 mm | 按操作员要求放大到 1 cm；按 Enter 前确认整个行程有空间。观察运动及读数，记录 +z 实际是向上还是向下。距离由 `config/limits.yaml` 的 `arms.arm_left.probe_z_jog_max_mm` 设置 |
| 胶带龙门 x -10 mm，再 +10 mm | 先 X- 再返回；按 Enter 前确认行程空间。首次方向由 `probe.gantry_x_first_sign` 设置 |
| 胶带龙门 z +5 mm，再 -5 mm | 用于确定 Z+ 的实际方向：**记录 Z+ 是向上还是向下**。先 +Z 再返回，不会低于起点；按 Enter 前确认上下都有空间。距离由 `probe.gantry_z_l2_mm` 设置 |
| 剥离加热台到 40 °C（20 V，最多 300 秒） | 15 V 时 300 秒内只到约 37–38 °C，按操作员要求提高到 20 V；到达 40 °C 后关闭输出；若 300 秒内未达目标，则关闭并报告失败 |
| 右桌面相机、显微镜相机拍照 | 同上 |
| 样品台真空 on | 泵启动，10 秒内自己关掉 |
| 右机械臂 z +10 mm，再 -10 mm | 同左机械臂；按 Enter 前确认整个行程有空间。距离由 `arms.arm_right.probe_z_jog_max_mm` 设置 |
| 样品台 x ±0.5 mm | 样品台横移一点又回来（在显微镜下时画面会横移） |
| 印章 z -5 mm，再 +5 mm | 反转此前测试方向；记录实际方向，并等待位置反馈确认完成；仍受 0–13 mm 行程限制 |
| 焦距 -5 mm，再 +5 mm | 操作员已确认安全空间；先向下再返回，每次估计等待约 30 秒，并拍前后显微镜照片 |
| 转移加热台到 40 °C（15 V，最多 300 秒） | 按操作员要求；到达 40 °C 后关闭输出；未达目标则超时关闭并报告失败 |

相机拍照时若图片窗口未打开，可用打印的路径手动打开。

- 通过：每步都是 `[OK]`，而且你都答了 `y`。
- 连接失败、动作前查询失败或 `[REFUSED ...]` 会记录并跳过该仪器，后续其他仪器仍逐步等待确认。
- 结束时会打印每步结果，并在 `runs/real-lab/probe/` 保存独立的 `*-l2-report.json`。软件返回 OK 与你的实际观察分开记录。
- 加热超时但未达到目标温度会报告失败；开始加热命令成功不代表加热测试通过。
- 出现 `LOCKED`：表示程序不知道仪器到底有没有照做（比如没回话）。程序会停下全部 L2 并进入安全状态。
  超时、停止请求和无法判断结果的异常也会停止整个流程。**先用眼睛检查那台仪器**，记下它的实际状态，再告诉 Claude，不要马上重试。

也可以单独测一个仪器（同样要按 Enter 确认，同样有上限）：

```bat
scripts\probe.bat switch stamp_vacuum on
scripts\probe.bat jog arm_left z 0.5
scripts\probe.bat heat exfoliation_heater 40 --timeout 300 --volts 20
scripts\probe.bat capture desk_camera_left
```

上限：两只机械臂 Z 和龙门 X/Z 为 10 mm（操作员指定；L2 龙门 Z 测试使用 5 mm）；机械臂 X/Y / 样品台为 0.5 mm，印章 Z 为 5 mm（其他印章轴 0.1 mm），焦距为 5 mm（操作员确认安全空间），开关 10 秒自动关，第一次加热最高 40 °C、
剥离加热台最高 20 V、转移加热台最高 15 V（操作员指定），必须写 `--timeout`。超过上限的命令会被直接拒绝。

## 第 7 步：做完以后

把整个 `runs\real-lab\probe\` 文件夹（日志和 L1 报告）和第 6 步里你记下的观察（方向、哪步答了 n）发给
Claude。Claude 会据此改 `config\` 里的端口和方向，并把确认过的项目从 `source: v1` 改成 `source: lab`。

调试网页（可选）：L2 结束后，关掉 probe，再运行 `scripts\start-lab.bat`，浏览器打开
http://127.0.0.1:8780/ 。勾选 "Take control" 才能动仪器；底部红色 STOP ALL 任何时候都能按。
probe 和网页不能同时开（COM 口只能被一个程序占用）。
