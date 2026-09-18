---
name: esp32p4-hw-debug
description: >-
  ESP32-P4-Function-EV-Board（openvela/NuttX）真机故障排障。当出现固件静默挂死、
  串口无输出、/dev/fb0 或 /dev/input0 不出现、defconfig 改动不生效、烧录后串口
  抓不到日志、触摸样本为空等症状时使用。含已验证的根因、修复与预防清单。
description_zh: "ESP32-P4 openvela 真机故障排障"
description_en: "ESP32-P4 openvela on-hardware troubleshooting"
---

# ESP32-P4 openvela 真机排障

## 何时使用

在 ESP32-P4-Function-EV-Board 上跑 openvela/NuttX 固件，出现以下任一症状：

- 上电静默挂死，串口（含 boot 日志）完全无输出；
- NSH 起来了，但 `/dev/fb0`、`/dev/input0` 等设备节点不出现，`dmesg` 为空；
- 改了 defconfig 但行为不变，构建异常快（秒级结束）；
- 烧录后串口抓不到日志、设备号漂移、读时报 disconnected；
- `hexdump /dev/input0` 长期 `npoints=0`，或 I2C 进入"所有写被拒"的卡死。

**不适用**：换芯片/换板、纯应用层问题、非 openvela 构建体系。

**板级事实**（排障前提）：芯片 rev v3.2；flash 16MB；32MB PSRAM @200MHz HEX；
只有 USB-Serial/JTAG（枚举为 `/dev/ttyACM*`，VID:PID `303a:1001`）；
显示 EK79007 1024×600（MIPI-DSI）；触摸 GT911 @ I2C0（GPIO7=SDA、GPIO8=SCL，地址 0x5D）。

## 起手三件事（症状无关）

1. **芯片是否存活**
   ```bash
   esptool -c esp32p4 chip-id      # 能读到 MAC = 芯片没坏、复位正常
   ```
2. **挂死还是复位循环**：连续观察 10 秒以上的设备状态。
   ```bash
   watch -n 0.5 'lsusb -d 303a:1001; ls /dev/ttyACM*'
   ```
   设备号**稳定** = 挂死（§1/§2/§5 方向）；**递增/反复消失** = 复位循环（多为启动崩溃）。
3. **看构建产物与链接警告**：链接期是否出现 `contains output sections; did you forget -T?`；
   在构建目录的 `nuttx.map` 里搜关键符号，确认调用链真的被链接。

## 症状索引

| 症状 | 小节 |
|------|------|
| 串口完全无输出、USB 设备号稳定，镜像烧录成功 | §1 固件静默挂死 |
| NSH 可用但设备节点缺失、dmesg 全空 | §2 板级初始化未执行 |
| 改 defconfig 不生效、构建秒级结束 | §3 配置未重新生成 |
| 串口抓不到输出、设备号漂移 | §4 USB 串口采集 |
| 触摸无样本、I2C 写被拒 | §5 GT911 触摸 |

---

## §1 固件静默挂死（串口无任何输出）

**现象**：编译 / 镜像 / 烧录全部成功；USB 枚举正常（`303a:1001`）且设备号稳定
（非重启循环）；串口 DTR 已拉高仍完全无输出，连 boot 日志都没有；`esptool chip-id` 正常。

**先排除镜像与烧录参数**（ADR-0005 的硬约束：任一条缺失 ROM 都无法加载，表现同样是
**静默挂死或反复复位**，必须先排掉再往下查）：
- 烧录偏移必须是 **`0x2000`**（P4 ROM 的引导偏移，区别于 C3/C6/H2 的 `0x0`）；
- `elf2image` 必须带 **`--ram-only-header`**（否则 ROM 段/SHA256 摘要处理错误），
  且 flash 参数为 **16MB / DIO / 80MHz**、总线 921600。

**定位**：
1. 排除控制台与 PSRAM：确认 `CONFIG_ESPRESSIF_USBSERIAL=y`、UART0 已关；临时去掉
   `CONFIG_ESPRESSIF_SPIRAM` 重烧，现象不变 → 排除 PSRAM 初始化崩溃。
2. 设备号不变 → 是稳定挂死，而非崩溃循环。
3. 回到构建日志：那条曾被忽略的
   `ld: warning: .../esp32p4_sections.rev3.ld.tmp contains output sections; did you forget -T?`
   是唯一显性线索。
4. 核对实际链接命令：
   ```bash
   grep -c -- '-T ' cmake_out/esp32p4-function-ev-board_nsh/build.ninja
   ```
   P4 需要 13 个链接脚本（ROM 函数表 ×8、peripherals、aliases、flat_memory、sections.rev3）。
   若 `-T` 数量远小于脚本数，就是它。
5. 最小复现：`target_link_options(t PRIVATE -T /a.ld -T /b.ld -T /c.ld)` 生成的
   `LINK_FLAGS` 是 `-T /a.ld /b.ld /c.ld` —— **CMake 对重复的选项标志去重**（3.23/3.31 一致）。

**根因**：构建脚本把"选项 + 脚本路径"交给 CMake `target_link_options()`，重复的 `-T`
被去重成一个，其余脚本被 ld 当作普通输入文件 → 段布局没有被应用 → ROM 加载后无法启动。
而 ELF 程序头看起来正常（`0x40000000` / `0x4ff40000` 由第一个脚本与 MEMORY 部分补救），极具迷惑性。

**修复**：顶层 `CMakeLists.txt` 的多脚本组装处，为每个预处理后的脚本显式拼接 `-T`：
```cmake
list(APPEND ldscript_tmp_list -T ${LD_SCRIPT_TMP})
```

**预防**：链接器警告必须当错误对待；涉及多链接脚本的构建变更，用
`grep -c -- '-T ' build.ninja` 核对 `-T` 数量与脚本数量匹配。

---

## §2 设备节点不出现（板级初始化未执行）

**现象**：NSH 正常启动，但 `ls /dev` 没有 `fb0`（只有 console/kmsg/null/random/ttyACM0/zero）；
`dmesg`（ramlog）完全为空。

**定位**：
1. 配置层：`.config` 中 `CONFIG_ESP32P4_MIPI_DSI=y`、`CONFIG_DRIVERS_VIDEO=y`、
   `CONFIG_VIDEO_FB=y` 都在。
2. 编译层：构建目录里 `esp_mipi_dsi.c.o`、`esp32p4_fb0_stub.c.o` 存在，
   `libboard.a` 里 `board_fb_initialize` 有定义。
3. **链接层（关键）**：在 `nuttx.map` 中搜符号——`board_app_initialize` 在，但
   `esp_bringup`、`esp_mipi_dsi_initialize`、`board_fb_initialize` 完全不在映射文件里
   → `--gc-sections` 把整条调用树裁掉了。
4. 读源码确认入口：树内 `esp32p4_appinit.c` 的 `board_app_initialize()` 是空实现
   （`return OK`）；真正的外设初始化在 `esp32p4_boot.c` 的
   `board_late_initialize()` → `esp_bringup()`，**只有 `CONFIG_BOARD_LATE_INITIALIZE=y`
   时才会被内核调用**。

**根因**：板级初始化有两条路径——NSH 应用层的 `board_app_initialize()`（树内为空壳，
`CONFIG_NSH_ARCHINIT` 走这条）与内核的 `board_late_initialize()`（真正 bringup）。
defconfig 缺 `CONFIG_BOARD_LATE_INITIALIZE=y` → 后者从未执行，所有板级外设初始化被跳过。

**修复**：defconfig 增加
```
CONFIG_BOARD_LATE_INITIALIZE=y
```
（保留 `CONFIG_NSH_ARCHINIT=y`，走空壳无副作用。）

**预防**：新板先确认板级初始化的真实入口；用 `nuttx.map` 验证关键调用链真的被链接
（编译进 `.o` ≠ 链接进固件）；空的 `dmesg` 本身就是信号。

---

## §3 defconfig 修改不生效（构建秒级结束）

**现象**：改 defconfig 后重跑构建 **3 秒**结束、产物几乎无变化、行为与改前一致；
检查 `cmake_out/.../.config` 发现新选项**根本不在里面**。

**定位**：对比构建耗时（正常全量 80~120 秒）；检查 `cmake_out/.../build.ninja`
时间戳未被刷新，配置未重新生成。

**根因**：CMake 在 **configure 阶段**由 defconfig 展开生成 `.config`；Ninja 只比较
源文件与产物的时间戳，**不知道 defconfig 变了**，因此不会主动触发重新 configure。
Make 路径有 `configure.sh -e` 的"配置变更即 distclean"逻辑，CMake 路径没有等价保护。

**修复（流程性）**：凡改动以下任一项，必须删除构建目录后全量重建：
```bash
rm -rf cmake_out/esp32p4-function-ev-board_nsh
./build_esp32p4.sh
```
适用范围：defconfig；esp-hal-3rdparty 头文件（尤其 `nuttx/esp32p4/include/sdkconfig.h`，
被大量源文件预编译缓存引用）；链接脚本与构建接线（Kconfig / Make.defs / CMakeLists）。

**补充坑**：若 configure 被中断（例如拉取 HAL 时），构建目录已建但缺 `CMakeCache.txt`，
之后所有构建都报 `Error: could not load cache`（`_do_cmake_generator` 只在目录**不存在**时
才跑 configure）。同样用 `distclean` 清掉整个目录恢复。

**预防**：把"改配置 = 全量重建"写成纪律；构建后
`grep <新选项> cmake_out/esp32p4-function-ev-board_nsh/.config` 确认生效；
构建耗时异常短（秒级）即说明没有真正重新配置。

---

## §4 USB 串口抓不到输出（DTR 门控与重枚举）

**现象**：`cat /dev/ttyACM0` 完全无输出但 minicom 有；烧录后立即读串口失败，
读一半报 `device disconnected`，设备号从 059 涨到 061。

**根因**：
- **DTR 门控**：USB-Serial/JTAG 只有在主机**拉高 DTR** 时才把固件输出转发给主机，
  未拉高期间固件写入的数据直接丢弃。`cat` 不控制 DTR → 永远无输出；烧录结束 esptool
  退出、DTR 释放后再打开端口，boot 阶段输出已经丢失。
- **复位引发重枚举**：hard-reset 及后续任何复位都会让 USB-Serial/JTAG 重新枚举 →
  已打开的句柄失效、设备节点消失再现、设备号递增；枚举完成前固件已在打印，boot 输出必然丢失。

**解决**：统一用带重连逻辑的采集脚本（本 skill 附带 `scripts/nsh_check.py`）：
```bash
python3 scripts/nsh_check.py                 # 连接并持续打印
python3 scripts/nsh_check.py --dump          # 连上后跑验证命令序列并退出
python3 scripts/nsh_check.py --reset         # 先复位再抓 boot 日志
```
要点：
- 按 **VID:PID `303a:1001`** 定位端口，**不要写死 `/dev/ttyACM0`**（设备号会漂移）；
- `dtr=True` / `rts=False`；不做任何可能把芯片拽进 ROM download 模式的 DTR/RTS 脉冲；
- 读循环捕获异常，等端口重现后重开，跨多次枚举持续采集；
- 需要 boot 日志时：先打开端口拉高 DTR，再触发复位，接受第一段丢失，以 ramlog 兜底
  —— `dmesg`（`CONFIG_RAMLOG_SYSLOG`）能取回 boot 阶段的 syslog；
- 交互调试用 `minicom -D /dev/ttyACM0`（自带 DTR 控制），退出 `Ctrl-A X`。

**预防**：团队统一用采集脚本，禁止依赖 `cat`；验证命令序列：
`uname -a` → `ps` → `mount -t procfs /proc` → `free` → `ls /dev` → `dmesg`。

---

## §5 GT911 触摸样本为空（确认帧与轮询架构）

**现象**：I2C 读写已通（产品 ID "911" 可读），但 `/dev/input0` 的 `npoints` 长期为 0，
触摸屏无事件上报；调试中多次出现"所有 I2C 写被拒、读正常"的卡死，只能断电恢复。

**三个根因（逐个击破）**：

1. **确认帧缺失 → 帧堵塞**。GT911 每次触摸/释放会锁存一帧并置位状态寄存器
   `0x814E` 的 bit7；主机**必须写 0 清除，否则控制器不再上报任何新帧**。早期只在
   "点数 ≥1"时才确认，导致一个 "bit7=1 但 0 点"（抬起事件）的帧永久堵塞后续触摸。
   **修复：bit7=1 的帧无条件确认**（无论点数），与厂商驱动一致。

2. **确认写的事务形态与重试策略**。本 I2C 主控**拒绝 3 字节数据写**（指针 + 值；用总线上的
   ES8311 对照确认：2 字节写成功、3 字节写 NACK）；对确认写做背靠背重试会把控制器打入
   "所有写被拒、读正常"的卡死状态（三个独立实现均观察到）。
   **修复：确认帧用 4 字节单事务**（指针 `0x814E` + 状态清零 + track 字节清零），
   **每帧至多一次、永不重试**，失败留给下个 20ms 轮询周期。

3. **I2C 流量与轮询架构**。应用按需 read 时总线长期静默，且 read 里的 I2C 与轮询相互竞争。
   **修复：20ms 周期的 HPWORK 轮询工作项独占所有 I2C 流量**（读状态 `0x814E` → 解码坐标
   → 确认 → 发布缓存样本 + poll 通知），应用 `read()` 只拷贝缓存样本、零阻塞。

**附带发现（同类问题易忽略）**：
- 驱动缺 `TSIOC_GETMAXPOINTS` ioctl → 输入框架无法分配样本缓冲、输入设备创建失败
  （报 `get touch maxpoints failed`）。实现后返回 1。
- `esp_i2c.c` 缺 `#include <nuttx/spinlock.h>` → `esp_i2c_reset`（`CONFIG_I2C_RESET` 路径）
  引用 `enter_critical_section` 导致链接失败。
- Kconfig 子选项被静默丢弃：挂在 `if DEBUG_INPUT` 块内的 `DEBUG_INPUT_INFO` 等，
  defconfig 只写子选项不写总开关会被忽略 → 对照 `.config`，不要只看 defconfig。
- I2C NACK 是常态：控制器内部刷新坐标缓冲期间会短暂 NACK，每次失败传输重试
  `CONFIG_INPUT_GT9XX_I2C_RETRIES`（默认 5）次、间隔 5ms。
- 地址锁存：控制器复位释放时按 INT 电平锁存 `0x5D` 或 `0x14`，主地址静默时尝试互补地址。
- 坐标镜像：面板装配方向与显示不一致，用 Kconfig 镜像开关在驱动层修正。

**验证**：NSH 下后台 `hexdump /dev/input0 &`，手指触摸时样本变为有效帧：
```
0000: 01 00 00 00 00 19 6d 01 b0 00 00 00
      npoints=1  id=0  flags=0x19(DOWN|ID|POS)  x=365  y=432
```
坐标随触点实时变化、松手恢复 `npoints=0`，且无 NACK 卡死复发。

**预防**：确认写永不重试（每帧一次）是防卡死的硬约束；面板类外设先 `hexdump`
看原始样本流再接上层框架；Kconfig 子选项确认总开关同时打开。

---

## 通用排障纪律（可迁移到其他 NuttX/openvela 板级）

1. **链接器警告当错误对待**，尤其 `did you forget -T?`；多链接脚本场景核对
   `-T` 数量与脚本数量匹配。
2. **用 `nuttx.map` 验证调用链真的被链接**：编译进 `.o` ≠ 链接进固件
   （`--gc-sections` 会裁掉无人引用的段）。
3. **改配置 / HAL 头文件 / 链接脚本 = 全量重建**，不要指望增量构建感知配置变化；
   中断过的 configure 留下的残缺构建目录要先清掉。
4. **串口采集必须控制 DTR、按 VID/PID 定位、支持跨枚举重连**，禁止依赖 `cat`。
5. **外设驱动排障先看原始样本流**（hexdump / 寄存器），再接输入、显示等上层框架。
6. **Kconfig 子选项必须同时确认总开关**，以最终的 `.config` 为准。
7. **外部依赖始终 pin 完整 SHA**，不用浮动分支；空日志（如空 `dmesg`）本身就是信号，
   说明目标代码根本没跑。
