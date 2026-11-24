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

