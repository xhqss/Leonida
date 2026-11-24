# 黑屏不是拦路虎：标题没有在渲染 —— 2026-11-25 第二轮

接手上文的会话记录。结论先写：**画面黑只是表象。真正卡住的是标题自己：它在启动期用约 10 秒
突发了 ~5 万个图形管线（全部保留、不释放，工作集涨到 16 GB），之后就进入一个"每秒只有几千次
D3D12 调用、但 CPU 13 核满载、GPU 3D 引擎只有 8%"的空转状态，既不再加载资源，也不再画场景。**
在这个状态破掉之前，任何针对"合成 draw #4 的矩阵"的修法都不可能让游戏"能玩"。

本文记录：新证据、上一份接手文档里需要修正的结论、本轮新增的探针、以及按优先级的下一步。

---

## 0. TL;DR

1. **场景渲染根本没发生**：整轮只有 **4 个 draw**，每个都是 3 顶点全屏三角形（后处理 blit 链），
   整轮只创建了 **63 个描述符**（heap 1 的 slot 0..103）。真实渲染一帧会创建成百上千个 view。
2. **启动期 PSO 洪水**：`CreatePipelineState` 在两秒窗口里被调用 **16,530 次**（5 个窗口，
   共约 5 万个），`live` 计数单调涨到 **49,588** 且从不下降 ⇒ 标题把它们全留着。
   约 8,185 个不同描述，之后 `0 new key(s)`、全是重复；所有调用都**返回成功**（`0 failed`）。
3. **洪水之后进入空转**：第 45 秒的实测是 CPU **429 s / 45 s ≈ 9.5 核**、工作集 **16.6 GB**、
   97 个线程，而逐秒的 D3D12 调用只有约 5,000 次
   （`CreateConstantBufferView` 416/s、`WaitFrameEventX` 416/s、SRV-from-raw-words 208/s ……）。
   **同一个进程读文件 0 字节**（`ReadOperationCount` 空、`ReadTransferCount` 0）
   ⇒ 它不是慢加载，是空转。
4. **GPU 几乎闲置**：`\GPU Engine(pid_…engtype_3d)\Utilization Percentage` = **8%**。
5. 线程采样（`tmp/thread-sample.ps1`）：10 个热点线程里 8 个在 `amdvlk64.dll`
   0x150e22x / 0x1fc… / 0x148… 各处自旋，1 个在 `ntdll+0x7439`。**瓶颈在驱动侧，不在 shim**。
6. 由此推论：**上一份文档把"合成 draw #4 的矩阵"当成唯一拦路虎是不成立的**。即使把
   draw #4 修好，它也只是把一张黑的中间纹理按正确的几何贴到屏幕上。

---

## 1. 上一份接手文档需要修正的地方

| 旧结论 | 本轮实测 | 影响 |
| --- | --- | --- |
| "黑屏的唯一原因是合成 draw #4 的顶点位置来自一个读不到的矩阵" | draw #4 确实一个像素都不写，但**连 PS 的 T# 也查不到**（§3），而且**没有场景渲染**可合成 | 优先级要整体下调 |
| "表内容按 gfx10 解释时约 75% 的槽位 base 超出 arena ⇒ 表格式还没对准" | 那张表**只有 3 个槽（96 字节）**：`+0x00` V#（矩阵）、`+0x20` T#、`+0x40` V#（顶点缓冲），正好对应合成 VS/PS 的读取偏移。按 64 KiB 去统计是**扫过了表尾、读到堆里无关数据** | 75% 这个数字是扫描 artifact，不是格式证据 |
| "表内容在帧间稳定 ⇒ 那 32 字节就是标题写好的最终描述符" | 写监视实测：表页**在整轮运行里没有任何人写**（§4）。内容来自加载期，两次运行完全不同 | 之前的"表内容"结论全部要重做，但优先级低（§6.2） |
| "帧率只有 1 帧/30 s 是 scratch 方案太慢" | 默认跑法本来就 3060 presents / 30 s；scratch 那次慢是因为**设备丢失**（`VkResult -4`），不是 memcpy 开销 | 别再优化表拷贝 |

---

## 2. 新工具：洋红兜底纹理（判定 bindless 命中/未命中）

`LEONIDA_FALLBACK_TINT=1`：把 bindless set 里 index 0 的 1×1 兜底贴图从黑清成洋红
（`runtime/src/vulkan_bindless.h` 的 `record_fallback_init`）。

判据很硬：

- shader 的 T# 哈希**查到**了 → 采样到真正的纹理 → 画面不是洋红；
- shader 的 T# 哈希**没查到** → 走 index 0 兜底 → **整屏洋红**。

```powershell
# 语料里的合成 VS 换成 tmp/composite-vs-tail.spv（全屏三角形），PS 保持原样
powershell -NoProfile -ExecutionPolicy Bypass -File tmp\run-probe.ps1 -Tag tint -Seconds 25 `
  -Set 'FALLBACK_TINT=1,CAPTURE=E:\temp\wait_work\Leonida\tmp\cap3,CAPTURE_LIMIT=3'
$py='C:\Users\xhqss\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
Get-ChildItem tmp\cap3\*.ppm | ForEach-Object { & $py tmp\ppm_stats.py $_.FullName }
```

**结果**：帧 31/61 = `min=0 max=255 non-zero 66.67%`（PPM 三字节里两字节非零 = 整屏洋红）。
也就是说：

1. 合成 PS 确实在跑、确实写了送显纹理；
2. 它的 **T# 哈希没命中**，采样到的是 index 0 兜底 → 黑屏。

这条坐实了上一份文档第十一轮的"哈希键契约"，并给了一个**每次实验都能立刻判读的 oracle**：
洋红 = 未命中，非洋红 = 命中。以后别再靠"帧是不是黑的"来猜。

---

## 3. 描述符链路的现状（重新对齐过的事实）

用 `LEONIDA_LOG_USERDATA` 打 draw 时的 user SGPR（push constant 前 16 个 dword）：

```
user data: 0 0 0 0 0 0 0 0 | 2f3eff80 0 0 0 | 0 0 0 0     draw #1
user data: 0 0 0 0 0 0 0 0 | 2f40ff80 0 0 0 | 0 0 0 0     draw #2
user data: 0 0 0 0 0 0 0 0 | 0b96ef00 0 0 0 | 0 0 0 0     draw #3
user data: 0 0 0 0 0 0 0 0 | 0b96dea0 0 0 0 | 0 0 0 0     draw #4
```

- **s8 = 表 VA、s9 = 0**，和语料里 `heap_base + ((s9<<32)|(s8&~3))` 的约定完全一致
  ⇒ **root signature 的 console 布局（base s8、每个 table 1 dword）是对的**。
  上一份文档里那条 "root parameter N does not fit … spill array not modelled" 的 fallback
  与这几条 draw 无关（是别的签名）。
- draw #1/#2 的 `s8` 是 4 MiB window 编码（它们走 root descriptor，不是表）；
  draw #3/#4 的 `s8` 是**原样交给层的表 VA**（默认没做任何表翻译）。
- 表 VA 换算成 heap 1 槽号：draw #3 = 368,504 + 4096·frame，draw #4 = 368,373 + 4096·frame；
  而层**整轮只写了 63 个描述符，最大槽号 103**（`LEONIDA_LOG_TABLE3` 的 `writes 63 maxslot 103`）。
- 表 VA 落在标题自己的一大块 `MEM_PRIVATE` 映射里
  （`VirtualQuery`：base 0x4e00000、size ≈ 1.04 GiB、PAGE_READWRITE），既不是 descriptor heap 的
  XMem backing，也不是 arena。

**结论**：着色器读 `heap_base + 0xb96dea0`（arena 偏移），那里是 0；标题的表在它自己的 1 GiB 堆里，
而**那一页在整轮运行中没人写**（§4）。两边从来没有同一时刻是同一块内存。

另一条实测：把 `GetGPUDescriptorHandleForHeapStart()` 改成返回 CPU handle
（`LEONIDA_XS_GPU_HANDLE_ARENA=1`，即 console 的"CPU/GPU handle 同一地址"语义）
**完全不改变表 VA**（仍是 0xb94ef00 / 0xb96dea0）⇒ **标题的表 VA 不来自这个 getter，
是它自己分配器里的地址**。

---

## 4. 写监视：表页在整轮运行里没人写

新增 `runtime/shims/write_watch.h`（`LEONIDA_WATCH_WRITE=1`，`LEONIDA_WATCH_SELFTEST=1` 自检）：

- `PAGE_GUARD` + VEH：`EXCEPTION_GUARD_PAGE` 里记 RIP（模块+RVA）与页内容"之前"，设 trap flag，
  `EXCEPTION_SINGLE_STEP` 里打"之后"并重新挂 guard ⇒ **每一次写都能抓到一次**；
- 自检（自己往被监视页写一个字节）确认机制真会触发，排除"没抓到 = 没写"的假阴性：
  `[rt] watch: write to page 0xbd0e000 ... by gta6rt.exe+0xe94a1`。

监视点选当前表 VA 之后 30/60/90/120 帧会出现的那几页（环每帧前进 0x20000）：

```
[rt] watch: page 0xbd0e000 armed
[rt] watch: page 0xc0ce000 armed
...（20–30 s 运行，环已走过这些页至少一遍）
（没有任何他人 write 记录）
```

⇒ **标题在运行期不写这些表页**。这同时解释了 `LEONIDA_LOG_TABLE_STABLE` 的
"word changed = 0" 和"bind 3000 时整张表全 0"。

补充：同一张表在不同运行的加载期内容完全不同（一次是 `{2,0x844,0xbc6,0x210440}`，
另一次是 `{0x8ed1e20,0,0x8ed1e20,0}` 这种指针对），说明那是**加载期堆里的数据**。

---

## 5. 本轮新增的开关与工具（全部 opt-in、默认关）

| 名字 | 位置 | 作用 |
| --- | --- | --- |
| `LEONIDA_FALLBACK_TINT` | `runtime/src/vulkan_bindless.h` | 兜底贴图索引 0 清成洋红；**bindless 命中/未命中的判据** |
| `LEONIDA_LOG_TABLE4=<stride>` | `runtime/shims/d3d12_xs_device.cpp` | 每 `stride` 次表绑定采样一次：表 VA、`VirtualQuery` 归属、前 10 槽 title/arena 两侧字节、以及**整张 64 KiB 的 32 字节槽普查**（zero / `{ptr,ptr}` / T# / 其他）与 T# 原文 |
| `LEONIDA_SLOT_PROFILE` | `runtime/shims/d3d12_xs_device.cpp` | 每秒一次"调用最多的 6 个 device/list slot"直方图；**空转时就用它看标题在调什么** |
| `LEONIDA_WATCH_WRITE` / `LEONIDA_WATCH_SELFTEST` | `runtime/shims/write_watch.h` | 页级写监视（§4） |
| `LEONIDA_XS_GPU_HANDLE_ARENA` | `d3d12_vulkan_device.cpp` + `d3d12_xs_device.cpp` | `GetGPUDescriptorHandleForHeapStart` 返回 CPU handle（统一内存语义）；表 VA 若落在 arena 映射内则还原成 arena 偏移 |
| `LEONIDA_XS_TABLE_MIRROR` / `LEONIDA_XS_MIRROR_BASES` | `d3d12_xs_device.cpp` | 把标题的表字节**按同一偏移**拷进 arena（需 `ARENA_MB=2048`），可选连 V# base 处的 4 KiB 一起拷 |
| `LEONIDA_LOG_PSO_SPAM` | `d3d12_vulkan_device.cpp` | `CreateGraphicsPipelineState` 的洪水/去重统计（本机恒为 0 次：标题走 stream 描述那条路） |
| `LEONIDA_LOG_PIPELINE_UNIQUE`（已有，本轮加了返回值统计） | 同上 | `N CreatePipelineState, X new key(s), Y repeat(s)` + `N created, M failed` |
| 断言现场 | `runtime/src/guest_asserts.cpp` | 跳过 guest `int3` 时打印 rcx/rdx/r8/r9/rax/rbx、返回地址、寄存器里能读到的 ASCII |
| `tmp/hexdump_rva.py` | `tmp/` | 只读地把 PE 某个 VA 范围打印成 hex（本会话的 python 子进程不能写文件，所以它打印而不落盘） |

也记下两个**已经排除**的方向，省后来者的时间：

- `XS_TABLE_MIRROR`（1:1 镜像 + 2048 MiB arena）：表字节进 arena 之后，合成 VS 立刻按真描述符
  解出一个 1.44 TiB 的 V# base → **device fault**（`console VA 0x1500ba4e000`）。
  "只要把表镜像进去就好"不成立：表里的 V# 不是按我们的 gfx10 解释写的。
- `XS_GPU_HANDLE_ARENA`：不改任何表 VA，画面仍全洋红。

---

## 6. 下一步（按优先级，别再按旧文档走）

### 6.1 先搞清楚"为什么没有场景渲染"（最高优先级）

已经确定的形状：

- 每轮只有 4 个 3 顶点全屏 blit，整轮只创建 63 个描述符；
- 启动期突发约 5 万个 PSO（全部保留，工作集 16 GB）；
- 之后每秒只有约 5,000 次 D3D12 调用，却烧 9–13 核 CPU，且全在 `amdvlk64.dll` 里；
- GPU 3D 引擎 8%，进程 0 字节文件读。

由此最可能的两类原因，按可查性排序：

1. **驱动侧在持续编译/自旋**（`amdvlk64.dll` 10 个热点线程）。用
   `LEONIDA_LOG_PIPELINE_TIME` 的 `graphics pipeline(s) built … live` 与
   `LEONIDA_SLOT_PROFILE` 同时开，确认空转期是否还有 `vkCreateGraphicsPipelines`
   （注意：`g_pipeline_states_live` 只统计 **shim 的 PSO 对象**，不统计层的
   `PipelineState::variant(unbound)` 变体 —— 变体是按 draw 的 unbound mask 惰性建的，
   如果 mask 每帧都不同，就会**每帧新建一个 VkPipeline**，而 shim 侧看不到。
   这条最值得先查：给 `variants_` 加一个 miss/新建计数并打印）。
2. **标题在等一个永远不会成立的条件**（`WaitFrameEventX` 每秒 416 次）。它现在被立即回答；
   `LEONIDA_SATISFY_WAITS=1` 试过，不改变行为。

另外那个唯一的 guest 断言值得定论（不跳过就 13 s 退出）：它出现在
`GetResourceAllocationInfo`（device slot 25）报出
"render targets sized as console tiles, 393216 B for 262144 B" 之后、swapchain 创建之前。
手工反汇编（`tmp/hexdump_rva.py`）出来的现场是：

```
140344190  83 7e 04 00        cmp dword [rsi+4], 0
140344194  74 01              je  +1
140344196  cc                 int3        <- 冒烟的就是这条
```

即标题在检查某个由设备调用填出的 16 字节结构里 `+4` 的 dword 必须为 0；极可能就是
"分配大小与它自己算的不一致"。值得把 `GetResourceAllocationInfo` 的返回值与标题的期望对齐。

### 6.2 之后：描述符表的真实格式

写监视已经证明运行期没人写表页，所以**先确认标题到底在哪里、什么时候构建这些表**——但这一步
要等 §6.1 解决、标题开始真正渲染之后再回来看，否则样本永远是加载期的陈旧数据。
候选做法：加载期就开 `LEONIDA_LOG_TABLE4` 与 `LEONIDA_WATCH_WRITE`，抓写表指令与写入值。

### 6.3 最后：才是合成 draw #4

等 §6.1/§6.2 之后，`LEONIDA_FALLBACK_TINT` 就是唯一判据：合成 PS 的 T# 命中（不再整屏洋红）
之后，再看 VS 的矩阵 V#（`+0x00`）与顶点 V#（`+0x40`）。

---

## 7. 回归基线与环境

- 构建：`Get-Process gta6rt | Stop-Process -Force; cmd /c tmp\build-gta6rt.cmd gta6rt`
  （改 header 之后仍要先 touch 所有 .cpp）。
- 默认跑法：`powershell -NoProfile -ExecutionPolicy Bypass -File tmp\run-probe.ps1 -Tag base -Seconds 30`
  → 本轮实测 **3060 presents / 30 s、0 device fault、0 present failed**。
- `ctest`（`E:\temp\wait_work\tools\cmake-4.5.1-windows-x86_64\bin\ctest.exe`，在 build 目录里跑，
  `ctest` 不在 PATH 上）→ 仍然只有那 6 个既有失败：`run_module_names`、`d3d12_com`(SEGFAULT)、
  `d3d12_vulkan_device`、`d3d12_depth_resolve`、`d3d12_pipeline_library_host`、`dxgi_swapchain`。
- 语料已还原并核对：
  - `)sga_dr_vs_pos_color_tex_c32788bc_Wrapped.spv` = `9B21E796017F529BD09CF9B5A2FF9B19F338459A0799093C3E6438184F808E46`
  - `default_ps_tex_c32788bc_Wrapped.spv` = `E231E529E80341216949C62E8127163AD3F7EFDBC8DA29E77247114F41A0BAA3`
- arena 上限实测：`LEONIDA_ARENA_MB=4095` → 被设备裁到 **2048 MiB**
  （`maxBufferSize` 与 `maxMemoryAllocationSize` 同时限制）。
- 判断"慢加载"还是"空转"的快捷方法（本轮就是靠它翻案的）：
  ```powershell
  Get-Process gta6rt | Select ReadOperationCount, ReadTransferCount, CPU, WorkingSet64, Threads
  Get-Counter '\GPU Engine(*)\Utilization Percentage' | Select-Object -ExpandProperty CounterSamples |
      Where-Object { $_.CookedValue -gt 1 } | Format-Table InstanceName, CookedValue
  pwsh -NoProfile -File tmp\thread-sample.ps1 -ProcessId <pid> -Top 10    # 必须用 pwsh 7，5.1 读不了带中文的脚本
  ```

---

## 8. 追加：第二轮的下一步做完了 —— 层的同步开销被排除，找到一个系统性错误

### 8.1 `LEONIDA_PROFILE_LAYER`：层自己一点都不忙

新探针（`runtime/src/layer_profile.h`）给四个"真的要碰驱动"的操作加了计时，每 2 秒打印
调用数 / 平均 / 最差（毫秒）。稳定期实测：

```
[rt] profile: submit       878 calls,    0.13 ms each, worst    0.38 ms
[rt] profile: present      439 calls,    0.60 ms each, worst    2.01 ms
（allocate / pipeline 只有在启动期才出现）
```

也就是说层每秒约 440 次 submit × 0.13 ms + 220 次 present × 0.60 ms ≈ **0.2 个核**。
而同一进程实测 CPU **12–13 核**、且 300 秒内线性增长（3692 s / 300 s = 12.3 核），
**永不衰减**；GPU 3D 引擎 8%。⇒ 那些 CPU 全部烧在 `amdvlk64.dll` 自己的线程里，
**不是 layer 的同步开销**，也不是"PSO 编译积压"（管线创建在头 ~10 秒的洪水之后完全停止）。

`tmp/thread-map.ps1` 把 96 个线程全部映射到 RIP：13 个在 100%，其中 6 个卡在
`amdvlk64.dll` 同一个 2 条指令的自旋循环里（`+0x77fe224` / `+0x77fe228`），
另有 1 个在 `ntdll` 里空转，其余在 amdvlk 各处。这是驱动内部的线程池行为，
**它的浪费是真的，但它不是"没有画面"的原因**（标题仍在 200 presents/s 地出帧）。

### 8.2 找到真正的系统性错误：**每一个 raw-word SRV 都是 null view**

设备 slot 70（Xbox "从原始描述符字创建 SRV"）每秒被调用 **400–500 次**。
`LEONIDA_LOG_SLOT70` 的计数结果是：

```
[rt] d3d12_xs: slot 70 in the last 2 s: 0 view(s) written, 576 null view(s)
[rt] d3d12_xs: slot 70 in the last 2 s: 0 view(s) written, 443 null view(s)
...
```

**成功率 0%。** 每次调用都落到 `write_null_srv`。null view 是着色器永远读不到的缓冲区。

原因（`LEONIDA_LOG_SLOT70=2` 打印的地址）：

```
slot 70 unresolved #1: va 0x4de0bf0000 (title 0x4de0bf0000) width 182452224, resource 0, committed 1
slot 70 unresolved #2: va 0x80001b80 width 160, resource 0
slot 70 unresolved #3: va 0x4de0c2de20 width 128, resource 0
```

`0x4de0bf0000` + 182,452,224 字节正是标题放在**它自己 XMem 堆**里的那个 placed buffer
（`slot 133 placed resource at VA 0x4de0bf0000`）。层的 `buffer_vas_` 按**层的 console VA**
（arena 偏移）索引，而标题给的是**它自己的主机地址**：两个地址空间，查不到，
所以 `resource_provider` 返回空 → null view。

### 8.3 已经落地的修法（默认开启，无帧率代价）

在 slot 70 里补上"标题内存"回退。**第一版用 `guest_translate` 的 4 MiB 窗口**：功能上立刻正确
（下面第一组数字），但标题的地址每帧前进一个窗口（`0x4de0c00000` → `0x4de1000000` …），
每个窗口是一个 4 MiB 的 host-visible buffer 且**永不释放** ⇒ 约 800 MB/s 的内存增长，
presents 从 ~106/s 掉到 ~52/s。所以那一版是 opt-in。

**第二版换成一个固定槽位池**（`raw_slot_copy`）：一个 1 MiB 的 mapped upload buffer，
切成 256 个 4 KiB 槽位；每次调用把需要的那几 KiB 拷进下一个槽位，view 直接建在池 buffer 上
（`FirstElement = slot * 1024`）。池的总开销固定 1 MiB，**没有任何新分配**；
一个槽位要经过 256 次调用才会绕回来（实测 ~450 次/秒 ⇒ 约 0.57 s），远大于 GPU 落后帧的时间。
现在**默认开启**（`LEONIDA_SLOT70_WINDOW=0` 可关掉做 A/B）。

效果：

```
第一版（窗口）： slot 70 in the last 2 s: 520 view(s) written, 38 null view(s)   presents ~52/s
                 之后       : 477 view(s) written, 0 null view(s)
第二版（槽位池）： 447 view(s) written, 0 null view(s)                            presents 104/s (= 基线)
```

**从 0 成功变成每秒 ~450 个真 view、0 个 null，且帧率与基线一致。** 这是本轮唯一真正
"修好一个功能"的改动；它让标题终于能把**放在自己堆里的 buffer** 作为 raw SRV 绑给着色器
（之前那些全是 null view）。

已知的边界（写在代码注释里）：view 只覆盖前 4 KiB（最多 1024 个元素），比标题声明的
`Width` 小；而且数据是**调用那一刻**的快照，标题随后再写那块内存不会反映过来。
比"整块 null"好，但仍不是完整语义。

`XS_TABLE_MIRROR` + `XS_DESC_SYNC` 这个组合也试过了：表字节进 arena 之后合成 VS 立刻按真描述符
解出 `0x80800000004` 这类 base → **device fault**（`console VA 0x80800000000`）。
所以"镜像表"必须先解决"表里的 V# 不是按 gfx10 写的"这件事，和 §8.2 是同一个地址空间问题的两面。

### 8.4 现在的优先级

1. **把同样的"标题地址 → 层资源"翻译用到描述符表上**（`XS_TABLE_*`）：表 VA 就是标题地址，
   和 §8.2 是同一套翻译，只是对象从 buffer 变成表。这一条打通之后，合成 draw 的 T# 才有可能命中。
2. **给 raw view 池补上完整语义**：现在只覆盖前 4 KiB 且是快照。要覆盖标题声明的完整 `Width`，
   需要按需把更多字节拷进更大的池（例如 64 KiB × 64 槽 = 4 MiB），或者按"提交序号"回收。
3. 然后才回到"表里的 V#/T# 是不是 gfx10 编码"（§6.2）——用 `LEONIDA_FALLBACK_TINT` 判读。
4. `amdvlk` 那 12 个自旋线程：`LEONIDA_PROFILE_LAYER` 抓到过
   `pipeline 4 calls, 4935.97 ms each, worst 18897.40 ms` —— 单次 `vkCreatePipelines` 阻塞
   最多 19 秒，所以那 12 核里至少有一部分是真正的 shader 编译，不只是空转。如果要优化，
   应该查那几条为什么这么慢（缓存未命中？巨型 PSO？）。

### 8.5 描述符表这条线的三个**否定**结果（省下次的时间）

用三个新探针把"表里的字节从哪来"逼到死角：

1. **`LEONIDA_XS_NO_HANDLE_TRANSLATE=1`**（不注册标题 XMem 的 descriptor range，让层像 console 一样
   把描述符直接写进标题自己的堆内存）之后，`LEONIDA_LOG_TABLE4` 看到的表内容**完全没变**：
   仍是 `00000043 5909e6d3 c747ec34 2b2bddd0 …` 这类数据，**没有一条**是层的 T# 编码
   （`01xxxxxx … 9xxxxxxx`）或 V# 编码（`… 300xxxxx`）。⇒ **标题不把 Create\*View 写出的描述符
   拷进它 bind 的那些表。**

2. **`LEONIDA_LOG_SLOTWRITE=1` 的新计数：`Create*View` 的目标落在"任何已注册 heap 之外"的次数是 0**
   （20 秒运行，一次都没有）。也就是说每一次 `Create*View` 都写进了 arena 里的 heap，
   **没有任何一次写进标题自己的内存**。整轮总量仍然是 **63 个描述符、最大槽号 103**。

3. 而标题 `SetGraphicsRootDescriptorTable` 的 VA 换算成 heap 1 的槽号是 **368,373 / 368,504 + 4096·frame**。
   ⇒ **标题 bind 的那些表槽，没有任何描述符 API 写过。** 这一条现在是有计数支撑的硬结论，
   不再是"没观察到"。

由此，"表里应该有什么"只剩两种可能：(a) 标题把描述符放进它自己内存的某个**不是我们看到的
那个数值**的地址；(b) 表由 GPU 侧写入（compute/copy），而对应的 arena 偏移是 0（所以读到零）。
这两种都要靠"把 `vkCmdCopyBuffer`/dispatch 的目标地址与表 VA 对起来"来区分，是本轮之后的第一件事。

### 8.6 本轮之后的第一件事（建议）

1. 在层里记录**每一条会写内存的命令**的目标 VA（`CopyBufferRegion`、`vkCmdCopyBuffer`、
   compute 写 UAV 的绑定），和表 VA 比对 —— 看那张表是不是 GPU 写的（可能性 b）。
2. 若都不是，就回到"标题自己算的地址是不是和我们算的一样"：在 `SetGraphicsRootDescriptorTable`
   里把 a2 与 heap 的 GPU handle 起点、与标题 XMem 堆的地址各算一次差值，看哪个差值稳定
   （0xb3ef00 这种）。差值的稳定性会直接指出标题用的是哪个基址。
3. raw view 池（§8.3）已经默认打开并修好了 buffer 侧；描述符表侧要走同一套翻译，才能让
   `LEONIDA_FALLBACK_TINT` 从"整屏洋红"变成别的颜色。

### 8.7 本轮新增开关

| 名字 | 位置 | 作用 |
| --- | --- | --- |
| `LEONIDA_PROFILE_LAYER` | `runtime/src/layer_profile.h` | submit / present / allocate / pipeline 的调用数、平均与最差耗时（每 2 s） |
| `LEONIDA_LOG_SLOT70` | `d3d12_xs_device.cpp` | `=1` 打印 raw-word view 的成功/null 计数；`=2` 还打印解析不到的地址、宽度、是否 committed |
| `LEONIDA_SLOT70_WINDOW` | `d3d12_xs_device.cpp` | raw view 的标题内存回退：**默认开**（固定 1 MiB 槽位池）；`=0` 关掉做 A/B |
| `LEONIDA_XS_NO_HANDLE_TRANSLATE` | `d3d12_xs_device.cpp` | 不注册标题 XMem 的 descriptor range，让层把描述符写进标题自己的内存（console 语义，§8.5） |
| `tmp/thread-map.ps1` | `tmp/` | 把进程**全部**线程映射到 模块+RVA，并按地址聚合计数（找自旋/锁竞争） |
| `tmp/thread-stack.ps1` | `tmp/` | 读最热线程的栈切片，统计栈上各模块的返回地址数（判断是不是层调进去的） |

---

## 9. 上游调研（2026-11-25 03:45，离线读 `origin/main`）

本地 `origin/main` = `6293c1c4f`（2026-11-24 13:38 -0500），本地 `HEAD` 领先 **129**、落后 **1394**。

### 9.1 同题结论：**上游的同一个卡点仍然 open**

- `docs/boot_leads.md` 的 **BL-4「descriptor words are 32-bit GPU addresses」状态仍是 open**，
  内容和上一份接手文档引的一样，并且给了实现方案：
  "census the descriptor words the title writes (count how many are valid addresses inside the arena and
  how many are not). If the census holds, translate a word to the buffer device address or bindless index it…"。
  ⇒ **我们不是漏了某个已合并的上游补丁；这一条上游也没做完。**
- `docs/retail_boot_chain.md` 的当前 stop 是 **13**，描述与我们的观察同一处：
  "right after the first `CreateDescriptorHeap`, the title calls the heap's slot 9
  (`GetCPUDescriptorHandleForHeapStart`), takes the handle from `rax` and asserts that it equals a value it
  already holds. Hypothesis: a console descriptor heap starts at an address the title chose or can predict,
  which the PC handle doesn't match"。

### 9.2 同一区域里上游**已经落地**的东西（可直接参考/摘取）

| 提交 | 内容 | 对本地的意义 |
| --- | --- | --- |
| `3630610ca` | console 形态的 `CreateDescriptorHeap`：16 字节 PC desc **之后** `+0x10`/`+0x18` 是堆的 console 地址与字节数；把它作为堆的 CPU/GPU 起点上报 | **本轮已用它验证了本地标题**（见 §9.3） |
| `a7ffc53c1` / `37246f11d` | 把 slot-14 的 console 堆创建走注册处理器；只有 descriptor size 相符时才采用该布局 | 后续接 `3630610ca` 时要用 |
| `da6270710` | **slot 133 placed buffer 按"标题放置的区间"登记，slot 70 先在这张表里查** —— 正是本轮 §8.2 那个 bug 的正式修法（完整 buffer、真实资源，优于本地的 4 KiB 快照池） | 值得摘；本轮尝试 cherry-pick，3 个文件冲突，已回退（见 §9.4） |
| `1d8d1d355` / `bc14ec5dd` / `d030b9912` | 上面那条的评审后续：锁内取引用、按 entry id 删除、mock 测试 | 一起摘才有完整语义 |
| `f9c68577e` | slot 133 的**纹理**用标题地址回答 `GetGPUVirtualAddress` | 和"标题地址优先"同一思路 |
| `9c8522d22` / `f9d20bf9b` | slot 70 的 raw-buffer view / null view 两条路径（本地已有） | 已对齐 |

### 9.3 本轮的新测量：标题确实传了堆地址，而且和我们拿到的一致

slot 14 的第一次调用（`LEONIDA_LOG_HEAPID` 之外新加的一行）：

```
[rt] d3d12_xs: slot 14 heap desc: type 0 descriptors 1245184 flags 0x1 node mask 0x1
      (raw 00000000 00130000 00000001 00000001); tail +0x10 0x4de00f0000 +0x18 0x2600000
```

- `+0x18` = `0x2600000` = 1,245,184 × 32 = **正好是这个堆的字节数**（和 PC desc 里的
  `NumDescriptors` 完全对得上）⇒ 上游那个"console 扩展布局"在**我们这个标题上也成立**。
- `+0x10` = `0x4de00f0000`，而 shim 的 XMem 分配钩子给回的 backing 也是 `0x4de00f0000`
  ⇒ 这一项**已经对齐**（`heap_get_cpu_handle` 现在返回的正是 `custom_cpu_start`）。
- 所以 stop 13 那个断言**不是**"CPU handle 值不对"这么简单；本轮把断言现场读出来了，见 §9.5。

### 9.4 cherry-pick 尝试（已回退）

`git cherry-pick -n da6270710` 在 `d3d12_xs_device.cpp`（4 处）、`.h`（1 处）、
`xs_proxy_mock_test.cpp`（3 处）冲突，因为本地这个文件已经大幅分叉。本轮**已把这些冲突改动全部回退**
（`git reset --hard` 到本轮自己最后的提交 `54b5c58f6`；只丢弃了我这次 cherry-pick 产生的冲突标记，
没有丢弃任何原本的工作）。要摘这条，建议按"手抄 registry"的方式重做，而不是直接 pick：

1. 在 `DeviceProxy` 里加一张 `console_va 区间 → ID3D12Resource*` 的表（不持引用，
   用 `SetPrivateDataInterface` 在资源析构时删条目，正如上游做的）；
2. slot 133 的三条创建路径（placed-at-VA 处理器、`CreateCommittedResource3`、`CreateCommittedResource`）
   都登记"标题放的区间"；
3. slot 70 先查这张表，再退回 `resource_provider`；
4. 然后就可以把本地的 4 KiB 快照池（§8.3）删掉 —— 上游版本给出的是完整 buffer 的精确 view。

### 9.5 那个唯一的 guest 断言，现在有内容了

`assert_registers` 现在会 dump `rsi/rdi/rbp` 指向的记录。**每次运行都一样**：

```
assert skipped: int3 in guest code at game_scarlett_master.exe+0x344196
assert registers: rcx 8efa26d567000000 rdx 0 r8 d62f40 r9 0 rax 4e7f6b8 rbx 13
assert pointer registers: rsi 4e7f730 rdi 4e7f6b8 rbp ffffffcd rsp 4e7f680
assert at rsi: ffffffff ffffffff 00010000 00000000 00000003 00000000 00000000 00000000
assert at rdi: ffffffff ffffffff 00010000 00000000 04e7fc76 00000000 00000001 00000000
```

断言指令是（手工反汇编，见 §6.1）：

```
140344190  83 7e 04 00   cmp dword [rsi+4], 0
140344194  74 01         je  +1
140344196  cc            int3
```

`rsi` 那个记录的第 2 个 dword 是 **`0xffffffff`**（标题要它是 0），前两个 dword 都是 `-1`，
后面是 `0x00010000`、`0`、`3`。也就是说：**标题在检查一个"描述符值"的第 2 个 dword 必须为 0，
而它读到的是 `-1`** —— 这是一次"我拿到的描述符不对"的自检，就在第一次
`CreateDescriptorHeap` + `GetCPUDescriptorHandleForHeapStart` 之后、swapchain 创建之前。
和上游 stop 13 是同一处、同一类判断。

⇒ **下一步的判据很明确**：让这个记录的第 2 个 dword 变成 0（即让标题读到的描述符是它期望的形状），
或者找出 `0xffffffff` 是从哪块内存读进这个栈记录的。前两个 dword 都是 `-1`，很像"未初始化的堆槽"
（本地 `write_null_srv`、或标题自己 memset 成 -1 的哨兵）。这条比 §8.5 的两个分支更容易直接验证。

**一个已经排除的可能（别重复做）**：它**不是** slot 70 的 null-view 描述符。
`LEONIDA_SLOT70_ZERO_NULL=1`（把 null view 改成写 32 个零 dword）之后，断言**在同样位置照样触发**、
记录内容一字不差。第一次在 20 秒窗口里"没看到断言"是假象——断言发生在 swapchain 创建之前，
那次 `Select-String -Last 6` 把日志行截掉了；60 秒运行里它照旧出现一次。
（保留该开关只为记录这个否定结果，并作为"无资源"的更便宜表达方式。）

**第二个已排除的可能**：它也**不是**"层把描述符写在 arena、标题回读自己的 XMem 副本"这个错位。
`LEONIDA_XS_DESC_SYNC=1`（`Create*View` 之后把 arena 里的 32 字节拷回标题那块内存）之后，
断言同样触发、记录一字不差。⇒ 这个记录里的 `0xffffffff` **不是**任何层写过的描述符槽。

### 9.6 顺带回答了上游 BL-4 里的那条"census"

BL-4 的实现方案是"census the descriptor words the title writes（统计多少是 arena 内的合法地址、
多少不是）"。我们这边已经做完了这个 census，而且是**否定**答案：

- `Create*View` 落在任何已注册 heap 之外的次数 = **0**（整轮，§8.5 第 2 条）；
- 整轮只有 **63 个描述符**被创建；
- 标题 bind 的表槽（368,373 + 4096·frame）**从来没有被任何描述符 API 写过**。

也就是说：**这个标题几乎不写描述符字**，所以"描述符字是 32 位 GPU 地址"这条假设在本作上
既无法被这些数据证实也无法被否证——要先解决"描述符到底在哪"（§8.5 的两个分支）。
把这条写进文档，是为了让下一个接手的人不要再去重做这个 census。

---

## 10. 这一轮踩到的坑（本轮新增，按代价排序）

1. **`Select-String ... | Select-Object -Last N` 会把关键行截掉，然后骗你得出"问题解决了"。**
   本轮我就因此错误地宣布"断言消失了"：断言发生在 swapchain 创建**之前**，而我只看了日志最后 6 行。
   正确做法：先按模式**计数**（`(Select-String -Pattern 'assert skipped' | Measure-Object).Count`），
   再看上下文。任何"某个症状不见了"的结论都必须用计数、并用足够长的运行时间（断言在 ~第 13 秒才出现）。
2. **`git reset --hard` 是本轮唯一一次动破坏性 git 命令**（为了清掉自己那次失败 cherry-pick 的
   冲突标记，HEAD 是刚提交的自己）。以后要么别用 `-n` cherry-pick，要么先用 `git stash`。
   注意 `git cherry-pick --abort` 对 `-n` 的 cherry-pick **无效**（会说 "no cherry-pick in progress"），
   而 `git checkout -- <file>` 对 unmerged 路径也会拒绝，只剩 reset 一条路。
3. **`ctest -j8` 不能和游戏运行同时跑**：本轮同时跑时多出 `d3d12_texture_tiles` 和
   `d3d12_bindless_churn_validated` 两个假失败，单独重跑都通过。跑 ctest 前先 `Stop-Process gta6rt`，
   并且不要并发跑探针。
4. **本会话的 Python 子进程不能写文件**（沙箱只给它读）：`tmp/exe_bytes.py` 落盘会
   `PermissionError`。所以工具改成**打印**（`tmp/hexdump_rva.py`）。PowerShell 子进程可以写。
5. **不要用 PowerShell 的 `Set-Content` 改项目文件**（本轮违例一次：`d3d12_xs_device.cpp`）。
   之后核对过：`git diff --stat` 只有预期行数、无 BOM、全 LF，未造成损坏；但以后一律用内置 edit 工具。
6. **`upload_translate` 只覆盖 upload chunk**。slot 70 那些地址是标题 XMem 堆里的地址，
   所以"加一句 upload_translate 就好了"不成立（本轮实测：翻译前后地址一模一样）。
7. **`guest_translate` 的 4 MiB 窗口不回收**：标题地址每帧前进一个窗口 ⇒ 约 800 MB/s 内存增长、
   帧率减半。任何"按地址建窗口"的方案都必须先做有界池（本轮 §8.3 的 1 MiB 槽位池就是答案）。
8. **`Select-String -Context` 的输出顺序会混**（`table3` 那种多条探针交叉打印时），
   对同一个 VA 的多份 dump 要按行号排序后再对照。
9. **断言探针要打 `rsi/rdi/rbp`**，不能只打 `rcx..r9`：这个标题的断言参数在 `rsi` 指向的记录里，
   前四个参数寄存器全是无关值（本轮补上之后才看清 `cmp dword [rsi+4], 0` 在读什么）。
10. **`LEONIDA_ARENA_MB` 的上限是 2048 MiB**（设备同时受 `maxBufferSize` 与
    `maxMemoryAllocationSize` 限制），不是文档里说的 4095；`4095` 会被静默裁到 2048。

---

## 11. 继续工作的入口（下一轮直接从这里开始）

本轮把 §9.5 的断言追到了**下一层**，这是目前最具体的一条线：

`0x1403440d0..0x1403441a0` 反汇编显示这段代码在做：

```
140344100  f6 47 30 03              test byte [rdi+0x30], 3          ; rdi = 一个 D3D12_RESOURCE_DESC*
14034410c  49 81 fc 07 00 08 00     cmp  r12, 0x80007
140344115  b8 00 10 00 00           mov  eax, 0x1000
14034412a  48 89 47 08              mov  [rdi+8], rax                ; desc->Alignment = 4096
14034413f  ff 90 c8 00 00 00        call qword [rax+0xc8]            ; 全局设备对象 vtable 的 slot 25
140344145  c5 f8 11 06              vmovups [rsi], xmm0              ; 把 16 字节返回值拷到 rsi
14034414e  48 81 7e 08 00 10 00 00  cmp  qword [rsi+8], 0x1000
140344190  83 7e 04 00              cmp  dword [rsi+4], 0
140344196  cc                       int3
```

- `0xc8/8 = 25` ⇒ 这就是**设备 slot 25 = `GetResourceAllocationInfo`**，和断言前那行
  `slot 25: render targets sized as console tiles, 393216 B for 262144 B` 对得上。
- 断言时 `rsi` 的 16 字节是 `ffffffff ffffffff | 00010000 00000000`，也就是
  **`SizeInBytes = UINT64_MAX`**（层里 `apply_console_placement` 把 `UINT64_MAX` 当作
  "PC 设备拒绝了这条 desc"的哨兵，直接 return）。
- ⇒ **真正的问题很可能是：某一次 `GetResourceAllocationInfo` 返回了 `UINT64_MAX`（无效），
  标题拿到它之后断言。** 不是描述符、不是 null view。

下一轮第一件事（10 分钟）：

1. 在 `proxy_get_resource_allocation_info` 里把**每一条返回 `UINT64_MAX` 的调用**连 desc
   （Dimension/Width/Height/DepthOrArraySize/MipLevels/Format/SampleDesc/Layout/Flags/Alignment）
   一起打印出来（前 8 条 + 每 500 条），确认是哪条 desc 被 PC 设备拒绝；
2. 再对被拒的那条 desc 逐字段和 `d3d12.h` 的合法性要求对照（Alignment 只有
   0 / 4 / 64K 三个合法值，而标题这里写的是 **4096** ⇒ 很可能就是 `Alignment = 4096` 导致
   PC 设备返回 `UINT64_MAX`；见 `alignment 4096` 这一项，它既不是 0 也不是 4 或 65536）。
3. 如果确认是 Alignment=4096：把 desc 的 Alignment 按 D3D12 规则归一化后再问 PC 设备
   （或自己按 console 的 placement 规则算），让标题拿到合法结果。

---

## 12. 第三轮：那个断言是**真修掉了**（不是跳过）

### 12.1 定位过程（15 分钟，按 §11 的入口走的）

新开关 `LEONIDA_LOG_ALLOC_REFUSED=1`：每次 `GetResourceAllocationInfo`（设备 slot 25）
返回 `UINT64_MAX` 就打一条，带 desc 的全部字段。第一次运行就抓到**唯一**一条：

```
[rt] d3d12_xs: slot 25 refused a description (#1, mask 0x1, 1 of them):
[rt] d3d12_xs:   [0] dimension 3 alignment 0x0 width 2560 height 1440 depth 1 mips 1 format 19
                     samples 1/0 layout 0 flags 0x6
```

（`dimension 3` = TEXTURE2D，`format 19` = `DXGI_FORMAT_R32G8X24_TYPELESS`，
`flags 0x6` = `ALLOW_DEPTH_STENCIL | ALLOW_UNORDERED_ACCESS`。）

然后同一条 desc 逐字段做**对照重试**（同一个探针里，每次去掉一个候选字段）：

```
as sent                          -> size 0xffffffffffffffff alignment 0x10000   ← 被拒
without ALLOW_UNORDERED_ACCESS   -> size 0x1330000        alignment 0x10000   ← 通过
without ALLOW_DEPTH_STENCIL      -> size 0xffffffffffffffff alignment 0x10000   ← 仍被拒
without either flag              -> size 0x12c0000        alignment 0x10000
as D32_FLOAT                     -> size 0xf00000         alignment 0x10000
```

⇒ **唯一原因是"深度格式上带 `ALLOW_UNORDERED_ACCESS`"**。PC 的 D3D12 不给任何深度格式 UAV 支持，
所以这条组合非法；console 允许，所以我们之前是"把非法描述交给宿主、拿到 `UINT64_MAX`、
标题拿去建资源、断言"。

而断言现场 `rsi` 的 16 字节正是 `{SizeInBytes = 0xffffffffffffffff, Alignment = 0x10000}`
—— 与 `as sent` 那一行的返回值一字不差。**§9.5 的"未知记录"到此闭环。**

### 12.2 修法

`runtime/shims/d3d12_xs_desc_clean.h` 新增：

- `is_depth_format(DXGI_FORMAT)`：9 个深度/模板格式（含对应的 TYPELESS）；
- `drop_depth_uav_flag(Desc&)`：格式是深度且带 `ALLOW_UNORDERED_ACCESS` 时丢掉该位；
- `pc_resource_desc()` 里调用它（覆盖 console→PC 的转换）。

并在三条会把 desc 交给 PC 设备的路径上统一应用：slot 25（`GetResourceAllocationInfo`，
含 PC-ABI 分支）、slot 108（`GetResourceAllocationInfo2`）、slot 127（
`CreateCommittedResource3X`）。理由：这是**宿主的规则**，不是某个 ABI 的规则。

### 12.3 验证

| 检查 | 修前 | 修后 |
| --- | --- | --- |
| `LEONIDA_LOG_ALLOC_REFUSED` 抓到的被拒 desc | 1 条（每次运行） | **0 条** |
| guest 断言 `game_scarlett_master.exe+0x344196` | 每次运行 1 次 | **0 次**（40 s 与 180 s 都没有） |
| **`LEONIDA_SKIP_ASSERTS=0`（不跳过断言）** | 13 秒后进程退出 | **跑满 120 s：22,980 presents、0 fault、0 断言** |
| 默认跑法 | 2880 presents / 30 s，0 fault | 3780 presents / 40 s（≈95/s），0 fault |

⇒ **标题不再需要 `LEONIDA_SKIP_ASSERTS`**。这一点很重要：那个开关会**静默吃掉**所有 guest 断言，
它之前一直掩盖着这个 bug。建议以后的回归基线都跑 `LEONIDA_SKIP_ASSERTS=0`，让断言露出来。

### 12.4 但画面还是黑的（诚实结论）

修完之后：描述符总数**仍是 63**（最大槽号 103）、每帧仍是 4 个 3 顶点 blit、
表槽仍是 368,373 + 4096·frame 且从未被写、抓帧 `min=0 max=0`。
也就是说：**这个断言只是"宿主给的分配信息非法"这条独立 bug，不是"没有场景渲染"的原因。**
§8.5 / §11 描述的"描述符表到底在哪"仍然是黑屏的主线。

### 12.5 顺手排除：`CreateLifetimeTracker` 的 10,240 次调用不是循环

新开关 `LEONIDA_LOG_FAILED_CALLS=1`（`d3d12_trace::result` 里的按方法计数，每 2 秒打印一次）
一开就抓到一个吓人的数字：

```
[rt] d3d12_xs: failed calls: ID3D12Device5::CreateLifetimeTracker ok 0, failed 0 (last 0x80004001), unsupported 1
[rt] d3d12_xs: failed calls: ID3D12Device5::CreateLifetimeTracker ok 0, failed 0 (last 0x80004001), unsupported 10240
（之后 30 秒内再无输出）
```

第一眼看像"标题每秒重试 5,000 次"。**但它只出现了一个 2 秒窗口，随后彻底停止** ——
和 §8.1 里那个 ~5 万个 PSO 的启动期洪水是同一件事（基本上是"每个 PSO 一次"）。
所以：`CreateLifetimeTracker` 返回 `E_NOTIMPL` **不是**渲染器停住的原因，不需要去实现它
（实现它只是把这个 API 补完整，对画面没有帮助）。**省下这个方向。**

---

## 13. 兄弟库调研（2026-11-25 04:25）：**同题已经有能跑的解法**

### 13.1 四个库的定位与实测状态

| 库 | 是什么 | 图形 API | GTA6 1.0.2.1 的状态 |
| --- | --- | --- | --- |
| **Leonida**（本库） | Xbox→PC 兼容层，D3D12→**Vulkan** | Vulkan | 启动、菜单、每帧 **4 个 blit**、画面全黑 |
| **victory-gta6**（`fxceless` 对 VIctory 的 fork） | Xbox→PC 兼容层，**自带着色器重编译器**（console ISA → DXIL） | **D3D12** | **世界能渲染出来**：2–4 fps，可玩约 13 分钟（其 README） |
| **AnyXboxX** | Xbox Series X 模拟器（Unicorn 跑标题代码），图形/shaders 直接用 victory-gta6 | D3D12 | 同上；`docs/running-gta6.md`：**每帧约 4,580 个 draw**、428 个 draw 因缺管线被丢 |
| **xcl** | Xbox 兼容层（Rust） | **D3D12** | 参考实现（console 描述符堆语义） |
| **five-star-recomp**（2026-11-25 新增调研，见 §14） | Xbox→PC 重实现（C#/.NET 10，GPL-3.0），**自己的 RDNA→HLSL 翻译器** + 自己的 loader + D3D12.X shim | **D3D12** | 它的 README：**"GTA VI does not run yet"**；能显示 logo/开场/菜单、载入故事模式到 title card，然后停 |
| ~~VIctory 上游~~ | 已被 fork 取代 | D3D12 | 用户明确说过时，忽略 |

**第一件必须记住的事**：两个能出画面的实现都是 **D3D12**，而且**自己编译着色器**。
Leonida 是唯一的 Vulkan 实现，且用的是**预生成的 SPIR-V 语料**（`heap_base + va` 契约被固化进
54,820 个文件里）。这决定了下面的取舍。

同样重要的一组对比数字：**victory-gta6 的世界每帧约 4,580 个 draw，我们每帧 4 个**
⇒ 我们的标题**根本没进场景渲染**，这和 §8.1 的结论一致，但现在有了外部参照。

### 13.2 它们解决"我们的卡点"的三个架构答案

**① 它们不改"地址模型"，它们改"着色器"。**
`victory-gta6/crates/victory-gpu/src/memory.rs` 头注释写得很清楚：给需要按**原始 40 位地址**
读写的着色器，做一张**目录** —— "per 64 KiB page of title address space the number of the copy
that holds the page (0: none) and the page's byte offset in it"，外加每个视图堆里一串 raw UAV
（每个 copy 一个），以及一份 **fault 表**（"A page that is not there when a shader touches it is
a lost access"）。因为着色器是它们自己重编译的（`victory-shader`），它们可以在着色器里
**查目录**而不是 `heap_base + va`。
⇒ Leonida 要做同样的事，只能改 `tools/rdna2spirv.py` 再重生成语料（这是本项目的"正解"，
但工作量最大）。

**② 它们**不让标题的描述符表进着色器**。**
`victory-gpu/src/descriptor/host.rs` 的 `mirror(index, handle, for_draw)`：宿主从**标题内存**里
`handle` 处读 12 字节记录 `{tag, i, generation}`（是宿主自己在 `Create*View` 时写进去的），
用它查自己的 `View` 记录，再 `make(&view, cpu(index))` 在**宿主自己的 shader-visible heap** 里
生成真 PC 描述符；`draw.rs` 的 `root_srv_table`/`root_uav_table` 最后 bind 的是
`host.gpu_handle(index)` —— **宿主堆的 handle，不是标题的 VA**。
⇒ 着色器读到的永远是宿主写的字节，所以不会有"表里是垃圾"这回事。

**③ console 描述符堆是"统一内存"**：`xcl/crates/xcl/src/hle/d3d12x/console_heap.rs` 里
槽位就在 GPU 可读内存里，**T# 的 word 0 是共享 PC 堆里的索引**，所以"描述符随便拷到哪都还指向它"。

### 13.3 给 Leonida 的具体思路（按性价比）

**I1（最便宜、且已经有一半代码）—— 让标题的描述符堆**就是** arena。**
这正是 `LEONIDA_XS_CPU_HANDLE_ARENA`：`GetCPUDescriptorHandleForHeapStart` 返回 arena 的 host
指针。这样标题自己写描述符就写进了 GPU 可见内存，表 VA 天然就是 arena 偏移，`Create*View`
也写在同一处，**三边天然对齐**（和 ①③ 的模型一致）。
本轮实测：它**不崩**，而且**走得比默认路径更远** —— 一路走到 `DeserializeComputePipelineStateX`
（设备 slot 67）才因为**语料缺一个 compute 程序**退出：
`no translated compute shader for FSPX kind-6 and kind-0 sha256 7a79b887…`。
⇒ 这条应该**继续调**，而不是像旧文档那样标成"会崩，未解决"。

**I2（不需要改翻译器）—— bind 表时把标题的表翻译成宿主自己的表。**
用 VIctory 的 `descriptor/host.rs`（`srv_desc`/`uav_desc`/`View`/`pc_format`，**按 10 个 dword**
解 console 描述符）当参考，解出标题槽位指向的资源，然后在 scratch 表里写**层的编码**再 bind。
这样着色器哈希的一定是层写进去的字节 ⇒ 必命中。**但前提是能读到标题的槽位内容**（见下面的否定结果）。

**I3（结构性，最大）—— 把 `rdna2spirv.py` 的地址模型改成"页目录"。**
照着 `memory.rs` 做：per-64 KiB 页目录 + 每个 copy 一个 raw UAV + fault 回收。这是唯一能让
**任意** 64 位标题地址可读的办法（我们现在受限于 2 GiB arena + `heap_base + va`）。
代价：重新生成 54,820 个 shader，并重写层里所有 `guest_translate`/window 相关代码。

**I4** xcl 的"4 GiB raw UAV 窗口"是 D3D12 专有原语，Vulkan 没有等价物；
Vulkan 侧的等价物就是 I3 的"大 buffer + 目录"。

### 13.4 本轮做掉的实验（都是否定结果，省下次时间）

| 实验 | 结果 |
| --- | --- |
| `LEONIDA_XS_TITLE_HANDLES=1`（层把描述符直接写进标题自己的描述符内存）+ `XS_TABLE_MIRROR` | ⚠ **本行结论已作废，见 §14.4**：这里的普查读错了地址（把 arena 偏移当 host 地址），"1836 个 `{ptr,ptr}`"是无关内存里的区间表。改正后：着色器读的位置**全零**，标题自己那份只有 7 个非零槽、里面是 drawable 头 `0x8e3/0xee4` |
| `LEONIDA_XS_CPU_HANDLE_ARENA=1` | 不崩；只 180 presents/24 s（慢），随后在 slot 67 因**缺 FSPX kind-6/kind-0 程序**退出 ⇒ **比默认路径走得远**，是新的线索 |
| 给窗口发 space/enter（`mcp__windows__` 输入工具，窗口标题 `Americas`） | 无变化；日志里**没有任何 GameInput/XInput 调用** ⇒ 标题**不是在等按键**，它没有轮询输入 |
| `LEONIDA_LOG_FAILED_CALLS=1` | 除 `CreateLifetimeTracker`（见 §12.5）外**没有别的被拒调用** ⇒ 没有第二个"静默失败"藏着 |

### 13.5 语料缺口（独立于描述符的线索）

所有配置下都有这两行：

```
[rt] d3d12: no translated compute shader for FSPX kind-6 and kind-0 sha256 7a79b887…
[rt] d3d12_xs: slot 67 call … failed with 0x80070057 (kind 0 28 B, kind 1 20 B, kind 6 296 B, kind 7 120 B)
```

那是 `DeserializeComputePipelineStateX` 拿来的一段 console compute 程序，语料里没有对应翻译。
victory-gta6 的经验是这类"程序没翻译"是**成片**的（它们修了 730 个 compute 程序的隐式 mip 采样）。
⇒ 值得用 `tools/rdna2spirv.py` 的诊断对这批 kind-6/kind-0 做一次普查，看缺多少、缺在哪。
**（第五轮做了一半：程序已经导出来并拆开，见 §15.1 —— 它是一个 76 字节的 FLT_MAX 填充内核；
反汇编在头部错位，所以翻译器拒绝。修的是反汇编，不是翻译器。）**

---

## 14. 第四轮（2026-11-25 05:00）：five-star-recomp + I1/I2 实做，**并纠正 §13.4 的一个假事实**

### 14.1 five-star-recomp：有用，而且是这五个库里**最完整**的那个

| 项 | 值 |
| --- | --- |
| 位置 | `E:\temp\wait_work\five-star-recomp` |
| 语言/构建 | C# / .NET 10 SDK，`dotnet build FiveStar.slnx`；GPL-3.0-or-later |
| 体积 | 2,044 个文件 / 7.5 MB |
| 图形 API | 桌面 **D3D12**（`src/FiveStar.Loader/Shims/D3D12X/`） |
| 着色器 | **自己的 RDNA→HLSL 翻译器**（`src/FiveStar.Shaders`，25,908 个程序全编译；可选自带 DXIL 后端，不用 dxc） |
| 它自己的状态 | README：**"GTA VI does not run yet."** 已到"故事模式加载 → 故事 intro 的 title card"，然后停 |

**它比 victory-gta6 走得远的地方**：完整 loader、资源/纹理/模型/音频格式、PSO 磁盘缓存、
DirectStorage、XG 布局与 detile、BCPack 纹理。**它没做到的地方**：跑不进游戏（victory-gta6 能出世界）。

对我们真正有用的是它的 **D3D12.X shim 的语义层**——它是唯一一个把"标题的描述符内存模型"
写成文档的库（`docs/d3d12x-shim.md` 66 KB + `docs/gpu-resources.md`）。

### 14.2 它给出的描述符模型（可以直接照抄的骨架）

1. **描述符堆 = 标题内存。** 标题的 shader-visible view/sampler 堆直接从标题自己的分配钩子
   （allocator id 128 / object type 4）取内存，所以 **CPU handle == GPU handle == 记录地址**，
   一个槽 32 字节，和 console 一样。
2. **记录 dword 0 = 桌面堆索引。** 槽 17..22 / 61 / 70 写描述符时顺手把 dword 0 写成
   "这个描述符在宿主 shader-visible 堆里的索引"，dwords 1..3 写 V# 的 stride / count / format。
3. **CPU-only 堆也这么做。** "the game creates eleven of 3,128 descriptors and copies their
   32-byte records into its per-draw tables itself, 0x144d9efc0"。
4. **descriptor window。** 一个 GPU 可读写的 buffer（write-combined 系统内存），按**游戏槽号**
   排的 32 字节镜像（前面留 sampler 记录、1024 条 null table、几个保留槽）。**bind 表时把
   window 的偏移当表指针传下去**，所以程序 `MemLoad(table + k*32)` 读到的是**镜像**，
   dword 0 就是它要的桌面索引。
5. **镜像时机：写描述符时 + bind 表时**，bind 时**至少 16 条、最多 1024 条**。原因写在常量注释里：
   "The game places a table's descriptors at the offsets of its own binding entries, not where the
   root signature's ranges say: `default_ps_tex` reads its T# at record 1 of a table whose root
   signature spans one."（`Binding/DescriptorWindow.cs` 的 `MinTableRecords`/`MaxTableRecords`）
6. **64 位地址另走一张页表**（`GameMemoryViews`）：per-64 KiB 页 → {raw SRV/UAV 对, 偏移}，
   写在一个 write-combined buffer 里，着色器查它。这和 victory-gta6 的 `memory.rs` 是同一招。

**和我们标题的对应关系（本轮实测，很有说服力）**：我们启动时创建的堆是
`heap 6..16`，每个 **0x18700 字节 = 3,128 × 32** —— **正好十一个**，和 five-star-recomp 记录的
"十一个 3,128 个描述符的 CPU-only 堆"完全对上。⇒ 两个标题用的是同一个机制，
**我们的 per-draw 表应该就是标题从这十一堆里拷过去的**。而我们的层把这些堆的记录写在
**PC 堆**里（`heap 6 ... real 0x4e251bb010`，不在标题内存），所以标题拷进表里的是 0。

### 14.3 I1 实测：不崩，但**光改 getter 不够**（原因已查明）

| 配置 | 结果 |
| --- | --- |
| `XS_CPU_HANDLE_ARENA=1` | 不崩；**180 presents / 30 s**；表 VA 仍是 `0x10b3ef00`；停在 slot 67（语料缺 compute） |
| `XS_CPU_HANDLE_ARENA=1` + `XS_GPU_HANDLE_ARENA=1` | 表 VA 变成 **`0x4de0c2ef00`** = 标题自己的 XMem 地址（type `0x40000` = `MEM_MAPPED`，base `0x4de0c20000`） |

**原因**：标题**不是**用 `GetCPUDescriptorHandleForHeapStart` 当基，而是用**它自己分配钩子
返回的地址**（`0x4de00f0000`）当 console 的"统一地址"基，再加上槽偏移算出表的 GPU VA。
所以只改 getter 的返回值**不能**把标题的写入搬进 arena。要让两边重合只有两条路：

- 让分配钩子返回 arena 内存 —— 做不到，`__XMemAllocationHooks__` 是标题自己的函数；
- **bind 时把标题那边的字节拷到 arena**（= `XS_TABLE_MIRROR`）或者
  **让 shader 读一个窗口**（= `XS_TABLE_VA`）。两者都已有开关。

### 14.4 I2 实测 + **两处旧结论纠正**（本轮最重要的产出）

**§13.4 里"表里仍是 1836 个 `{ptr,ptr}`、0 个 T#"是测量错误。** 原因：`table4` 普查用
`copy_committed(t, table_va, …)`，把 arena 的**偏移**当成了 host 地址去读，读到的是
`0x5000000` 起的那段**无关内存**。已修：普查现在读 `arena_host_base + a2`（a2 = 实际交给
层的值 = 着色器真正读的偏移），并额外打一份"标题自己那份"。

改正之后的事实（`LOG_TABLE4` 普查 2048 槽 × 32 B × 每个环槽）：

| 读的是谁 | 内容 |
| --- | --- |
| **arena + a2**（着色器真正读的位置） | **2048/2048 全零**，每一帧、每个环槽都一样 |
| **标题自己那份**（`0x4de00f0000 + slot*32`） | **2038 零 + 7 个"T#" + 3 个 other** |
| 那 7 个非零槽 | 里面是 **`0x8e3` / `0xee4` 两个头** —— five-star-recomp 文档写明这是
slot-133 池里 **drawable 的头**（`0x8e3` = 位置从 +16 起、28 字节一个；`0xee4` = 颜色从 +28 起），
**不是描述符** |
| 被误当成"表内容"的 1836 个 `{ptr,ptr}` | 是 `{start, end}` 指针对，**大多数 start == end = 空区间**
—— 那是标题低地址内存里的一张**区间/分配器表**，和描述符无关 |

⇒ **结论翻转：黑屏不是"描述符编码对不上"，是那张表里根本没有描述符。**
我们前三轮一直在追一个不存在的东西。

其它实测：
- `XS_TABLE_MIRROR=1`（把标题的表按同样偏移拷进 arena）：arena 变成那 1836 对，
  但**只 1 present 就停** —— 拷进去的是区间表，装不了描述符。
- `XS_TABLE_VA=1`（把表 VA 换成标题页的窗口）：窗口偏移 `0x2fa0ef00` **超出 arena 的
  `0x10000000`**（窗口在另一块映射里），同样 **1 present 就停**。
- 两个"能读到真 T#"的历史记录（§11 说 `LOG_ROOT` 能看到真 console T#）也建立在这个错读之上，
  **不要再用它当依据**。

### 14.5 I3 评估（本轮**不执行**）

照 `memory.rs` 把 `rdna2spirv.py` 的 `heap_base + va` 换成 per-64 KiB 页目录 + raw UAV 对，
再重生成 54,820 个 shader。这仍然是**能让任意 64 位地址可读的唯一办法**，但它解决的是
**缓冲区地址**，**不解决"表里没有描述符"**。→ 在 14.4 的结论下，它不该是下一步。

### 14.6 下一步（按价值重排，替换 §11 的入口）

1. **回答"为什么只有 4 个全屏 blit"** —— 这是现在唯一真问题。已知线索：
   - 整轮只创建 **63 个描述符**（真实一帧要几千个）；
   - 每帧 4 个 3-vertex 全屏 blit；串通 14.9 的链路后，表里只有 1 个 V#、**没有采样的 T#**
     ⇒ 画的是**空**，而且这是标题的本意（黑屏，不是我们画错）；
   - `DeserializeComputePipelineStateX`（slot 67）**每轮都失败** `0x80070057`，
     缺 `FSPX kind-6 / kind-0` 的 compute 程序（§13.5）；
   - 12.3 核 CPU、GPU 3D 引擎 8%、12 个 amdvlk 线程自旋 ⇒ 标题在**等/重试**，不是在建场景。
   ⇒ 先修 slot 67 的语料缺口（用 `LEONIDA_DUMP_SHADERS` 把那批程序导出来，再
   `tools/rdna2spirv.py` 补翻译）。victory-gta6 修了 **730 个**这类程序，说明这是成片的。
2. ~~**把 11 个 CPU-only 堆的记录写进标题内存**（five-star-recomp 模型的第 1、3 条）~~ ——
   **14.9 已经做掉了**（`XS_DESC_SYNC_CPU`），实测有效。该项剩余的部分：现在只有 32 次 sync
   被调用（每轮 32 个视图），说明标题确实只建了这么少的视图，不是我们的路径漏了。
3. **bind 时按"至少 16 条"镜像，而不是按根签名 range**（五个库共同的坑）：`mirror_table` 现在
   固定 64 KiB，已经覆盖；如要更严格，可改成取根签名 range 的上界。
4. 描述符链路本身**暂时冻结**：14.9 已经证明它通，剩下的瓶颈不在这一层。

### 14.7 关于"D3D12 退路"的评估（用户提出的备选）

- 三个能跑到"有画面/有世界"的实现（victory-gta6、AnyXboxX、five-star-recomp）**全是 D3D12**，
  而且**全都自己编译着色器**。这不是巧合：描述符槽就是 GPU 内存里的 32 字节记录，
  着色器直接把 dword 0 当索引读，**根本不需要"把描述符字节塞进 bindless 哈希表"这种发明**。
- 我们这条路（预生成 SPIR-V + 8-dword FNV 哈希 + 2 GiB arena）每一个设计点都和 console 语义
  有点偏差，每个偏差都要单独修。
- 但**换成 D3D12 不是"改个后端"**：要重写 `runtime/shims/d3d12_vulkan_device.cpp`
  的全部资源/管线/描述符/障碍物语义，还要把 `rdna2spirv.py` 的输出从 SPIR-V 换成 HLSL/DXIL
  并重生成 54,820 个程序。参考实现在那里（five-star-recomp 是 GPL-3.0，可以直接读它的
  `docs/d3d12x-shim.md` 当规格书），但这是一个**以周计**的工程，不是一轮能做完的事。
- 折中的中间态：**保留 Vulkan 后端，但把描述符模型换成 five-star-recomp 的 window 模型**
  （14.2 全部 6 条都能在 Vulkan 上实现：window 就是一个 storage buffer，
  着色器查它 → 拿 bindless 索引 → 索引 `images[]`/`samplers[]`）。
  **这条不需要改后端，只需要改 `rdna2spirv.py` 的 `desc_lookup` 入参来源**：
  现在它哈希"标题的 8 个 dword"，改成"从 window 里读 dword 0 当索引"。这是最小改动、
  且和已经验证可行的模型一致。→ **推荐这一条，而不是换 D3D12。**

### 14.8 本轮的工具改动（都已提交）

- `table4` 普查修正：读 `arena_host_base + a2`（原来错读 `table_va` 当 host 地址），
  并新增"标题自己那份"的独立普查 + `arena_host_base/size` 打印。
- `mirror_table` 修正：源地址原来是 `table_va` 当 host 地址（错），现在走
  `title_address_of()` 把 arena 偏移换成标题自己的 console 地址再拷。
- **V#/T# 判别式修正**（这是一个贯穿全层的隐性 bug）：原来用
  `(dword3 >> 28) & 0xf != 0` 判 T#，但 gfx10 **V#** 的 dword 3 是
  `format | OOB_SELECT(28-29) | ADD_TID_ENABLE(30)`，`OOB_SELECT = 3` 的 V# 顶 nibble
  就是 3 ⇒ **每个这种 V# 都被当成 T#**。标题的 composite 表里 V# 全是 `0x30014fac`，
  正好中招。T# 的 type 只有 **8..15** 合法，判别式改成 `>= 8`。
  （普查、`sanitize_table_window` 等处都用这个判别式，所以"0 个 T#"的历史读数也不可信。）
- 新增开关：`LEONIDA_XS_DESC_SYNC_CPU`、`LEONIDA_XS_MIRROR_VBASE`；无默认行为变化。

### 14.9 本轮最后跑通的链路（**描述符管线第一次真正接通**）

三级开关串起来，每一步都有实测证据：

| 步骤 | 开关 | 实测证据 |
| --- | --- | --- |
| ① 层把描述符字节写进**标题自己的内存**（只对 **CPU-only 堆**，不动 shader-visible 堆） | `XS_DESC_SYNC_CPU=1` | `[rt] desc sync 1 V#: e0c2ff20 0000004d 00000060 30014fac base 0x4de0c2ff20 committed 1` —— 标题的 per-draw 表里出现了**层的编码**（此前只有 0） |
| ② 标题的表按**正确源地址**镜像进 arena | `XS_GPU_HANDLE=1` + `XS_TABLE_MIRROR=1` | `[rt] table mirror graphics: title 0x4de0c2ef00 -> arena 0xb94ef00 (65536 bytes)`；arena 普查从 **2048/2048 全零**变成真实条目 |
| ③ 表里 V# 的 console 地址换成 arena 偏移 | `XS_MIRROR_VBASE=1` | `[rt] table mirror: +0x0 V# base 0x4de0c2ff20 -> arena 0xb94ff20`；arena 槽 0 读作 `0b94ff20 00000000 00000060 30014fac` |

**结果**：`60 s → 2220 presents`（37 Hz）、**0 assert、0 device fault**、6720 次 V# 改写。
只开 ②（不开 ③）时**只有 1 个 present** 就停 —— 因为 V# base 还是 64 位标题地址，
着色器的 `heap_base + base` 直接飞出去。③ 是让整条链路活下来的关键。

**但窗口仍然是纯黑**（`mcp__windows__Screenshot` 抓 `Americas` 窗口确认）。
原因现在很明确、也很干净：那张表里 **2048 个槽只有 1 个 V#（顶点/常量）和 0 个被采样的 T#**，
标题**根本没打算采样任何东西**；整轮 63 个 `Create*View` 也说明它没有资源可画。
⇒ **黑屏是"标题还没画东西"，不是"我们画错了"。** 描述符链路到这里可以算**通了**，
剩下的不是描述符问题。

**默认配置无回归**：30 s → 2760 presents、0 assert、0 fault（基线 2880–3180）。

### 14.10 决策记录：要不要换 D3D12（用户问的"第六点"，整理成表）

| 路线 | 工作量 | 风险 | 结论 |
| --- | --- | --- | --- |
| 继续现在的 Vulkan 层 | 每轮小步 | 每个设计点都和 console 语义有偏差（预生成 SPIR-V / 8-dword FNV 哈希 / 2 GiB arena） | **继续**，但要按 §15 换焦点 |
| **中间态：保留 Vulkan，把描述符模型换成 five-star-recomp 的 window 模型** | 改 `rdna2spirv.py` 的 `desc_lookup` 入参来源（"哈希标题的 8 个 dword" → "从 window 读 dword 0 当索引"），重生成语料 | 中 | **推荐**（最小改动、模型已被三个库验证） |
| 整层换 D3D12 | 重写 `d3d12_vulkan_device.cpp` 的全部资源/管线/描述符/障碍物语义 + 把翻译器输出从 SPIR-V 换成 HLSL/DXIL + 重生成 54,820 个程序 | 高，**以周计** | 不建议现在做；参考实现是 five-star-recomp（GPL-3.0，`docs/d3d12x-shim.md` 可直接当规格书） |

**为什么"换后端"看起来诱人**：三个能出画面的实现（victory-gta6、AnyXboxX、five-star-recomp）全是
D3D12 且都自己编译着色器 —— 描述符槽就是 GPU 内存里的 32 字节记录，着色器直接把 dword 0 当索引读，
不需要"8-dword FNV 哈希"这种发明。**但 §15 的证据说明当前的黑屏不是描述符或后端造成的**，
所以换后端不会更快出画面。

---

## 15. 第五轮（2026-11-25 05:20）：slot 67 修了，**但黑屏不是它**

### 15.1 缺的那个程序是什么

`LEONIDA_DUMP_SHADERS` 导出的 payload 用 `tools/fspx_unpack.py --disasm --backend python` 拆开：

- 目录：kind 0（28 B，dispatch state）、kind 1（20 B）、**kind 6（296 B，RDNA2 程序）**、kind 7（120 B）。
- kind 0 解得：**threads 32×1×1、wave32、8 vgprs、6 user sgprs、LDS 0** —— 都正常。
- kind 6：真正是代码的只有前 **76 字节**，后面 50 个 dword 全是 `s_nop`(`0xbf9f0000`) 加尾部填充。
  有效部分是**一个 8 条指令的内核**：

```
s_mov_b32 s2, s5
s_mov_b32 s3, 0
v_lshl_add_u32 v0, s6, 5, v0
s_load_dwordx4 s[0:3], s[2:3], 64
v_mov_b32_e32 v1, 0x7f7fffff        ; FLT_MAX
s_waitcnt lgkmcnt(0)
buffer_store_dword v1, v0, s[0:3], 0 idxen
s_endpgm
```

⇒ **它是"把一段缓冲填成 FLT_MAX"**（深度/hi-Z 或 min/max 清零一类），不是关键内容。

- **它是成片的**（和 §13.5 的猜测一致）：单独跑 `tools/rdna2spirv.py` 时 1 个程序 0 个翻译，
  失败原因 `s_nor_b64 source` —— 而且那个 `s_nor_b64` 是**反汇编错位**：kind-6 头部
  dword 0/1 (`0xd580beb7 0x03feb731`) 和 dword 2 (`0x8daff260`) 被当成两条 8 字节 VOP3，
  解出 `v_nop_e64` / `s_nor_b64 s[47:48], s[96:97], 1.0` 这种不可能的指令。gfx1013 与 gfx1030 结果相同。
  ⇒ **下一轮要修的是 `tools/fspx_unpack.py`/`shader_disasm.py` 对这个头的处理**（是不是有 prologue
  没跳过：`.json` 里写的是 `"prologue": false`），不是翻译器缺功能。

### 15.2 `LEONIDA_XS_EMPTY_CS`：先让管线建出来，看标题到底在等什么

新增开关：语料里没有翻译的 compute 程序，**不再拒绝管线**，而是用内置的
"立刻返回"的 SPIR-V compute 模块（`LocalSize 1 1 1`、无绑定、无 LDS，35 个 dword，`spirv-val` 通过）
建出管线，并把事件记进日志。理由：一个建不出来的管线是**标题在等的东西** ——
slot 67 每帧重试 302 次，后面什么都不跑。

**实测（`XS_EMPTY_CS=1`，70 s）**：

| 指标 | 默认 | `XS_EMPTY_CS=1` |
| --- | --- | --- |
| `slot 67 ... failed with 0x80070057` | 302 次/40 s | **0** |
| presents | 2760 / 30 s（92 Hz） | **10620 / 70 s（152 Hz）** |
| assert / device fault | 0 / 0 | 0 / 0 |
| **标题碰到的 D3D12 入口点数量** | **85** | **85（完全一样）** |

⇒ **管线建成功了，标题不再重试，帧率还涨了 65% —— 但它的行为一点没变，画面仍然是黑的。**
所以 slot 67 是**真缺口、真修掉了一块**，但**不是黑屏的原因**。

### 15.3 更锐利的诊断：**标题的 GPU 端根本没开始干活**

把这一轮所有 `first call` 汇总（85 个入口点）能读出三件决定性的事：

1. **从不 dispatch compute**：`list slot 61`（Dispatch）**从未出现**。
   （建了 compute 管线：slot 62/67；但一次都没 dispatch。）
2. **从不画索引几何**：只有 `list slot 12`（DrawInstanced），**`list slot 13`（DrawIndexedInstanced）从未出现**。
   ⇒ 每帧那 4 个 3-vertex 全屏 blit 就是全部。
3. **从不读文件**：`dstorage.cpp` 是完整实现（真 `ReadFile`），但标题**一次都没 enqueue**；
   之前 300 s 的采样也是 0 次文件读。DirectStorage 只建了 factory/queue 就再没用过。

再加两个数字：

- `ExecuteCommandLists` 只有 **~0.54 次/秒**（38 次 / 70 s），而 `PresentX` 是 **152 次/秒**
  ⇒ **呈现线程在没有新工作的情况下空转**，它重复呈现同一份空内容。
- CPU：97 个线程、**1658%**，其中 **12 个线程各占 ~99%**、1 个占 **384%**，
  而 GPU 3D 引擎只有 8% ⇒ 一堆线程在**自旋等待**，不是在算东西。

⇒ **结论：标题卡在"资产加载之前"的 CPU 侧**。它把 D3D12 初始化完、画了个空 quad、就再也没往前走；
GPU 侧（描述符、黑屏）是**症状**，不是原因。§14 花整轮追的描述符链路确实通了，
但它服务的对象本身没在干活。

### 15.4 下一步（按价值，替换 §14.6）

1. **归因那 13 个满核线程**（最高优先）。`tmp/thread-map.ps1` 这次拿到的
   `rip=unreadable`（97 个线程全部读不到 RIP），所以还不能归因；先修这个脚本
   （或改用 `thread-stack.ps1` + `dbghelp`），把 RIP 落到
   **标题自己 / amdvlk64.dll / 我们的 gta6rt.exe** 三者之一。若多数落在 amdvlk64.dll，
   那是 152 Hz 无工作呈现把驱动的自旋线程撑着（我们的成本），可以先降 present 频率验证。
2. **音频三连**：`xapu.dll!XApuConnect`、`chad.dll!XDspConnectWithMaximumStreamLimit`、
   `xapu.dll!XApuGetResultCount` 都是 `unimplemented`。一个从来不初始化音频的标题
   很可能不会开始游戏（GTA 的启动序列包含音频设备初始化）。
   **这是"标题不往前走"最可疑的单点。**
3. **`xg_xs` 的 `XGSuggestSwizzleMode` / `XGCreateTextureComputer` 返回 E_NOTIMPL**。
   five-star-recomp 的 `docs/xg.md` + `XgCopier`/`ConsoleAllocationSizes` 是完整参考
   （我们已经在槽 25 里算 console tile 尺寸，说明布局数学有一部分）。
   如果标题要靠它们准备流式纹理，这里失败就等于没有纹理可画。
4. **修 kind-6 头的反汇编**（§15.1），把缺的 compute 程序补进语料 —— 做完再关掉 `XS_EMPTY_CS`。
5. 描述符链路（§14.9）保持现状，**不要再投入**，除非 1/2/3 有进展。

---

## 16. 第六轮（2026-11-25 06:10）：**13 个满核线程是驱动里的管线编译竞争，已修掉**

### 16.1 先做了只有进程内才能做的工具：`LEONIDA_THREAD_MAP`

外部脚本 `tmp/thread-map.ps1` 对 97 个线程**全部**读到 `rip=unreadable`（跨进程 `OpenThread` +
`GetThreadContext` 拿不到这个进程的上下文），所以归因不了。改成**进程内**采样器
（`runtime/src/thread_sampler.cpp`，`LEONIDA_THREAD_MAP=<秒>`）：

- 挂起每个其它线程 → **一次** `GetThreadContext` → 立刻恢复；CPU 时间取 `GetThreadTimes` 的差值，
  所以"谁在烧 CPU"是测出来的而不是猜的；
- **栈用 `RtlVirtualUnwind` 真走**：loader 早就把每个映射镜像的 `.pdata` 用 `RtlAddFunctionTable`
  注册过，所以主机 loader 没见过的 console 代码也能 unwind。（先试过裸扫栈，192 和 512 个 dword
  都找不到一个标题帧，全是 ntdll 的残留值。）
- 地址解析走 **loader 自己的表**（`find_address`）：宿主的模块列表里根本没有映射进来的镜像，
  这也是外部脚本读不到 RIP 之外的另一半原因；
- `gta6rt.exe` 的帧用 **dbghelp + 322 MB 的 PDB** 解成函数名（`SymInitialize(..., TRUE)`）。

### 16.2 它查出来的东西（决定性）

满核的 13 个线程，栈底全是 amdvlk64.dll，**再往下就是我们的层**：

```
game_scarlett_master.exe+0x24800db      <- 标题调用 slot 67
gta6rt.exe proxy_slot<67>+0x9
gta6rt.exe proxy_create_compute_pipeline_from_native_code+0x249
gta6rt.exe Device::CreateComputePipelineState+0xde
gta6rt.exe PipelineState::create_compute+0x832      <- vkCreateComputePipelines
amdvlk64.dll ... (19 帧)
```

而标题**自己的**线程（98 个里的绝大多数）全部是 parked：栈上是 `game_scarlett_master.exe+0x2215eca`
（线程引导）加 1~4 个标题帧，`ntdll` 的等待原语在最上。⇒ **烧 CPU 的不是标题，是驱动；
标题的线程都在睡。** 之前"标题在自旋"的判断（§15.3）**是错的**。

### 16.3 根因：**管线创建没有串行化，也没有去重**

`LEONIDA_LOG_SLOT67` 的普查给出了这个"风暴"的真实形状（一个 25 s 的 run）：

```
call 13: code 43a0d4fb60d07951 rs 0x7cce7ec0 kind6 384 B
call 14: code ba4afc9be3028c43 rs 0x7cce7ec0 kind6 576 B
call 15: code 8ea9ce4b880bbccd rs 0x7cce7ec0 kind6 768 B
...
```

同一个 root signature，**每次的程序都不一样**，大小在 384/512/576/640/704/768/832/1024 之间循环。
⇒ 这是 FSR/upscaler 那一族的**有限**管线集合，**不是重试循环**（所以 §15.2 的 `XS_EMPTY_CS`
让它"不再重试"的说法要修正：它本来就不是重试）。标题用 **13 个 worker 线程同时**建这些管线，
而 amdvlk 内部**串行**编译 ⇒ 12 个线程在驱动里**自旋**等锁，一烧就是整场。

**修法**（默认生效，无开关）：`proxy_create_compute_pipeline_from_native_code` 里

1. 一把 `std::mutex` **跨** `CreateComputePipelineState` 调用持有 —— 等锁的线程在内核里**睡**，
   而不是在驱动里自旋；
2. 同一把锁下按 `(字节码 sha256, root signature)` 做**管线缓存**：重复的程序直接复用同一个
   `ID3D12PipelineState`（D3D12 允许一个 PSO 服务任意多次），根本不进驱动。

### 16.4 实测

| 指标 | 修前 | 修后 |
| --- | --- | --- |
| CPU（`XS_EMPTY_CS=1`，稳态） | **1609%**（13 线程在驱动里） | **231%** |
| presents（40 s） | 4800 | **10560**（264 Hz） |
| presents（默认配置 30 s） | 3060 | **6360**（212 Hz） |
| assert / device fault | 0 / 0 | 0 / 0 |
| 标题碰到的 D3D12 入口点 | 85 | **85（没变）** |

⇒ **这是一个真的性能 bug，修完 7 倍 CPU、2 倍帧率**；但入口点集合依旧不变，
所以**它也不是"标题不往前走"的原因**。描述符、帧率、管线风暴现在都可以从嫌疑名单上划掉。

注意：绝对数字取决于那 10 秒窗口落在启动的哪一段。管线风暴这一段的特征是
`XS_EMPTY_CS=1` 时 CPU 从 1609% 掉到 231%；而**默认配置**（slot 67 直接失败、风暴根本不存在）
同一窗口只有 368% —— 也就是说这 1600% 只在"管线能建出来"的时候出现，
这也反过来证明它确实是**管线编译**而不是别的东西。

### 16.5 顺带做掉的：帧节拍 `LEONIDA_XS_PACE`（默认关）

`WaitFrameEventX` 原来立刻返回一个新令牌，所以标题**自由跑**：同一份空帧每秒呈现 106–152 次，
每次都是一次 submit + present。`LEONIDA_XS_PACE=1` 按标题自己 `SetFrameIntervalX` 要的间隔
（boot 要 33333 µs）睡到下一个"帧起点"，这才是 console 上 vblank 的形状。

实测：present 从 3720/35 s 降到 **420/30 s**，而 **CPU 一点没降（仍 1437%）** ——
这条正好**证明** §16.2 的结论：满核与"呈现太频繁"无关，是管线编译竞争。

### 16.6 现在的嫌疑名单（按价值）

1. **标题在等什么**：它的线程全都 parked，最外层是 `game_scarlett_master.exe` 里的等待。
   下一步用新的采样器把 **`game_scarlett_master.exe` 的 RVA 反汇编**（`tmp/hexdump_rva.py` +
   `docs/` 里的反汇编笔记）查那 10 个 parked 调用点，尤其是**最深的那个**
   （`+0x249987c`/`+0x249a3c0` 那条 8 帧链）—— 那是标题主循环在等的东西。
2. **0 次文件读**仍然成立：标题初始化完 GPU、建完 ~300 个 compute 管线、画 4 个空 blit，
   **从不发起一次 DirectStorage 请求**。资产加载根本没开始。
3. 音频三连（`XApuConnect` / `XDspConnect` / `XApuGetResultCount`）各只被调用一次，是初始化，
   不是轮询 ⇒ 优先级降到 3。
4. `xg_xs` 的 E_NOTIMPL（`XGSuggestSwizzleMode` / `XGCreateTextureComputer`）仍是候选，
   但如果资产加载没开始，它还没轮到。
5. `XS_EMPTY_CS` 还开着（默认关）：语料仍缺那个 FLT_MAX 程序（§15.1），
   修 `tools/fspx_unpack.py` 的头部反汇编后再补。

---

## 17. 第七轮（2026-11-25 06:40）：**图形管线风暴（50,432 个）+ 两条旧结论被推翻**

### 17.1 先纠正两条：标题**读过盘**，而且读了很多

上一轮把"0 次文件读 ⇒ 资产加载没开始"写进了嫌疑名单。**这是错的。** 用进程 I/O 计数器量
（`Win32_PerfRawData_PerfProc_Process` 的 `IOReadOperations`/`IOReadBytes`）：

| 时刻 | 读操作 | 读字节 |
| --- | --- | --- |
| 稳态 t0 | 833,589 | **3,764,196,950**（3.76 GB） |
| 25 s 后 t1 | 833,595 | 3,764,506,540 |
| 增量 | **+6 次** | **+310 KB** |

⇒ 标题在启动阶段**读了 3.76 GB**，然后**几乎完全停止**（25 秒里 6 次、310 KB）。
所以"没读盘"只在**稳态**成立：**它已经把该读的读完/停在某个点上**，
而不是"还没开始读"。DirectStorage 的 enqueue 计数仍然为 0（那条另有解释：它的读走的是
同步 `ReadFile` 路径，不是 storage queue）。

### 17.2 真正的 CPU 大头：**图形管线每 2 ms 建一个**

`LEONIDA_LOG_PSO_COUNT`（新增）在槽 86 上计数。一个 ~71 秒的 run 里：

```
graphics pipeline state #49408 / #49664 / #49920 / #50176 / #50432
```

⇒ **50,432 个图形 PSO，约 710 个/秒，连续不断**。而 compute 侧只有 64 个
（§16 那 300 个是 FSR 的一族，早建完了）。

对照第一轮就记下的"~5 万个 PSO 的启动期洪水"——**那个数字是真的，而且它不是"启动期"**：
它一直在建。这也是 §16 里"CPU 全在驱动里"的真正来源（每个 PSO 都要 amdvlk 编译一次）。

### 17.3 修法：图形 PSO 也串行化 + 按流去重（默认生效）

`proxy_create_pipeline_state`（槽 86）：

- 按 **pipeline state stream 的 sha256** 做键（stream 就是整个 PSO 定义，字节相同就是同一个状态），
  命中就 `AddRef` 并把第一次建出来的对象还回去；
- **不**把锁跨驱动调用持有 —— 这一条是实测出来的：先按 compute 侧的做法加了跨调用的锁，
  默认配置的 present 从 **6360/30 s 掉到 300/30 s**。原因是标题那 ~48,000 个状态**几乎全不同**，
  串行化只是**把 13 路并行变成 1 路**，一点也没省；去掉跨调用锁、只留缓存后回到 **4620/30 s**。
  compute 侧相反（§16：加锁 4800→10560/40 s），两边不对称，是量出来的，不是推的。
- `LEONIDA_LOG_PSO_COUNT` 每 4096 次调用打印 `calls / distinct / hits`：distinct ≈ calls 说明
  标题**确实有这么多不同的状态**（预编译整库），distinct 很小说明它在反复要同样的东西。

### 17.4 答案：**标题确实有 5 万个不同的 PSO**（不是重复要同一个）

`LEONIDA_LOG_PSO_COUNT` 的 census（每 4096 次调用一行，40 秒的 run）：

```
4096 calls, 4026 distinct, 69 hits
8192 calls, 8115 distinct, 76 hits
...
49152 calls, 48205 distinct, 946 hits
```

**49,152 次调用里 48,205 个是不同 的（hits 只占 1.9%）** ⇒ 标题在**预编译整库**，
不是泄漏也不是反复要同一个。每个 PSO 在这边都是一次 SPIR-V 编译，在 console 上只是
"把 native 代码拼成状态"，这就是为什么它在这边慢到 3 分钟还没建完。

**这是本轮最重要的结论**：标题不是"卡住"，是**在干一件这边比 console 慢上千倍的事**。
把它跑完需要的时间 = 50,000 × 每次编译耗时；想让它"能玩"，几乎肯定需要
**跨运行的 PSO 缓存**（five-star-recomp 也有 "a disk cache"，它的 README 明写
"its pipelines build from the translated shaders (with a disk cache)"）。

### 17.5 试过但**失败**的：把 Vulkan pipeline cache 存回磁盘（已回滚）

层里**已经**会在启动时读 `pipeline_cache.bin`（`create_seeded_pipeline_cache`，带设备身份校验），
但**从来不写**（`pipeline_cache.bin` 根本不存在）。于是加了两处：

- `vkGetPipelineCacheData` → 原子写回 `pipeline_cache.bin`（在 `gta6rt.exe` 旁边，不动游戏包）；
- 每 2048 次建成就存一次（这样被 kill 的 run 也能攒下成果）。

**结果：进程在启动早期 32 秒时 `0xc0000005` 崩掉**，稳定复现。
最可疑的是把 `vkGetPipelineCacheData` 加进了 `LEONIDA_VK_FUNCTIONS` 那张表
（加完就崩，回滚就好）；另一个已知的真实隐患是 **VkPipelineCache 没有外部同步**，
周期性地在别的线程正在 `vkCreateGraphicsPipelines` 时把它交给 `vkGetPipelineCacheData`
是未定义行为。**两个隐患都要处理**再重做，所以整块回滚了，
只留下 `note_pipeline_created()` 的计数思路在注释里。

### 17.6 踩到的坑（本轮）

**不要在测量跑动的时候 build。** `tmp\build-gta6rt.cmd` 前面那句
`Get-Process gta6rt | Stop-Process -Force` 会把正在跑的长测**杀掉** ——
一个"200 秒"的 run 就这样在 71 秒被自己终止，`run-probe.ps1` 报的是
`exited on its own after 71s`，而日志里没有任何退出原因（因为根本不是它自己退的）。
判据：日志末尾在 present 中途截断、没有 assert/exit 行 ⇒ 先怀疑自己。**这一轮犯了两次。**

---

## 18. 第八轮（2026-11-25 06:30）：**给足 8 分钟也没用 —— PSO 预编译在 49,152 处停住，标题进入空转**

### 18.1 15 分钟不打断的 run（`tmp/long3-*.log`）

| 时刻 | graphics PSO | presents | 入口点 | CPU |
| --- | --- | --- | --- | --- |
| ~2 min | 45,056（44,405 distinct） | — | 85 | — |
| ~3.5 min | **49,152**（48,355 distinct） | 42,840 | 85 | — |
| ~8 min | **49,152（不再增长）** | 91,500（≈195 Hz） | **85** | **239–242%** |

⇒ **预编译在大约 2–3 分钟时结束**（计数停住），之后标题进入一个**固定点**：
每帧 4 个空 blit、~195 Hz 呈现、2.4 核 CPU、**入口点集合一个都没多**。
**"再多跑一会儿" 这条路到此为止：给它 8 分钟和给 40 秒是同一个状态。**

### 18.2 稳态那 2.4 核在干什么（新的、具体的账）

进程内采样器在稳态抓到的两个最热的线程：

```
80%: ntdll+0x166844 ← KERNELBASE+0xce450 ← leonida::write_committed+0x56
     ← console_write_value+0x97 ← console_extension_call+0x94 ← list_slot<72>
     ← game+0x2482b37 ← game+0xa1c36c ← game+0x24842a0 ← … ← game+0x2215eca
56%: ntdll+0x166464 ← KERNELBASE+0x1427f ← game+0x56a0d ← game+0x4e7f358 ← game+0x6259
     ← game+0x2481f73 ← game+0x2215eca
```

- 第一条是**标题在用命令列表的 slot 72（bottom-of-pipe qword 写）轮询内存**，
  而我们的 `leonida::write_committed`（`runtime/src/guest_memory.h:44`）**每次写都做一次
  `VirtualQuery`**（`KERNELBASE+0xce450` 就是它）。标题高频调用 ⇒ 单线程 80% 核。
  **这是一个明确的、便宜的优化点**：给同一地址窗口的重复写做一个很小的区域缓存。
  本轮**没有做**——它有 crash 风险（页面被 decommit 后仍写），且它不解决"标题不走"，
  只省 CPU。
- 第二条停在 `game+0x2481f73`，紧邻 slot-67 的调用点（`+0x24800db`），也就是管线族那一带。

### 18.3 compute 侧那把锁：**保留**（两侧不对称，两端都量过）

| 配置 | CPU | presents |
| --- | --- | --- |
| compute 侧**跨驱动调用持锁**（当前） | **231–244%** | 10560 / 40 s（264 Hz）、5940 / 40 s |
| compute 侧**只缓存不持锁** | **1332%** | 5460 / 60 s（91 Hz） |

⇒ compute 侧持锁是**对**的（13 个 worker 同时进 amdvlk 会互相自旋）；
graphics 侧不持锁是**对**的（48,000 个状态几乎全不同，串行化只砍并行，§17.3）。
两个方向都实测过，写进注释，避免下次又"统一一下"。

### 18.4 关于剩下三个 lead 的**评估**（读了代码，没改）

- **`xg_xs`**（`runtime/shims/xg_xs.cpp` 头部注释）：`XGSuggestSwizzleMode` 的返回值
  **没有任何调用点分支**；`XGCreateTextureComputer` 的 4 个调用点里 2 个检查、
  2 个不检查 —— shim 已经**故意**在失败时仍写 `*out`，所以不检查的那两个不会跳空指针。
  ⇒ 它是"少了一条纹理布局路径"，不是硬失败。**优先级低**。
- **音频三连**：每个只被调用一次（不是轮询），且音频其余部分（`ApuCreateHeap`/`AcpHal*`/
  `ApuAlloc`/HAL 对象 16 个 vtable 全 S_OK）都通。⇒ **不是"卡在音频初始化"**。**优先级低**。
- **kind-6 头部反汇编**：只影响那一个 FLT_MAX 程序（`XS_EMPTY_CS` 已经绕过）。**优先级低**。

### 18.5 现在真正的问题只剩一个

**标题在 49k PSO 建完、数据读完、GPU 空闲之后，为什么不往下走？**
它的 85 个入口点一个没多、`Dispatch` 从未出现、`DrawIndexedInstanced` 从未出现，
所以它**没有进入渲染/计算场景**。剩下的可能只有两类：

1. **标题自己的一条启动条件没满足**（某个资源/服务/状态），它在等；
2. **它在等一个永远不会来的输入或事件**（上一个会话测过按键无效、且没有 GameInput 轮询）。

要把这一类问题定位下去，下一步需要的是**标题代码侧**的手段，而不是 GPU 侧：
- 把稳态那 ~5 个 parked 线程的调用链（`+0x249987c` / `+0x247d413` / `+0x81b2e8` /
  `+0x53ec05e` 那条 8 帧链最值得看）用反汇编读出来；
- 或者给 `write_committed` 之外再加一个"**我们的 shim 里谁被高频调用**"的普查
  （`LEONIDA_LOG_CALL_RATE`），找出标题在**轮询**哪个 API —— 轮询的对象就是它在等的东西。

