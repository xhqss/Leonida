# 接手文档：把 GTA VI（Xbox 1.0.2.1）在 PC 上跑起来 —— 2026-11-25

> **⚠ 2026-11-25 第二轮更正（先读这条，再往下看）：**
> 本文把"合成 draw #4 的矩阵"当成唯一拦路虎，这个判断**不成立**。下一轮实测发现：整轮只有 4 个
> 3 顶点全屏 blit、只创建了 63 个描述符、**没有任何场景渲染**；启动期突发约 5 万个 PSO 后标题就进入
> 空转（CPU 9–13 核、GPU 3D 引擎 8%、进程 0 字节文件读）。另外 §0 的"表内容 75% 槽位不可达"是
> 扫描 artifact（那张表只有 3 个槽），"表在帧间稳定"是"运行期没人写表页"的结果。
> **请先读 [2026-11-25_title_spins_on_pipeline_state.md](2026-11-25_title_spins_on_pipeline_state.md)**，
> §7 的优先级以那份文档为准。本文的证据与踩坑清单仍然有效。

写给下一个接手的人（或下一个会话的 AI）。目标不是"像素完美"，**唯一验收标准是游戏能正常开起来、能玩**。
黑屏是现在唯一的拦路虎。本文只记录能直接用的结论、命令和下一步；细节证据在
[docs/2026-11-24_black_frame_and_descriptor_tables.md](2026-11-24_black_frame_and_descriptor_tables.md)（第一～十三轮，
中文，含每次实验的原始日志片段）。

---

## 0. TL;DR

- 游戏已经能启动、加载、进菜单、**稳定 present（每秒 60 帧级别的 present 调用）**，但**送显的纹理全 0**，
  所以窗口是黑的。默认跑法无 device lost、无错误。
- 黑屏的直接原因链已经查清到最后一层：**合成 draw（draw #4）的顶点位置来自一个 4×4 矩阵，矩阵由一个 V# 描述符
  指向的 buffer 提供；着色器按 `heap_base + 表VA + 偏移` 读描述符表，而表 VA 是标题自己的内存、不是 arena**，
  于是着色器读到 0 → V# = 0 → 矩阵 = 0 → 位置退化 → 一个像素都不写（已用"强制洋红 PS"证明：连洋红都出不来）。
- 把表的字节拷贝到 GPU 能读的地方（`LEONIDA_XS_TABLE_SCRATCH`）之后表能读到了，但仍有两个卡点：
  1. 一个固定的 device fault：`console VA 0xd1e7dc27000`（不经过 `shader_address`，来源未定位）；
  2. 表内容按 gfx10 解释时约 75% 的槽位"base 超出 arena"，说明我们还没对准表的真实格式/真实来源。
- 详细下一步见 §7。

---

## 1. 机器与固定路径

| 项 | 值 |
| --- | --- |
| 仓库 | `E:\temp\wait_work\Leonida`（分支 `main`，最新本地提交 `f87a51099`） |
| 构建目录 | `runtime\build-561b65a`，产物 `runtime\build-561b65a\gta6rt.exe` |
| 游戏 | `E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK\game_scarlett_master.exe` |
| 语料 | `E:\temp\wait_work\leonida-work\shaders\spirv-561b65a`（54,820 个文件） |
| 兄弟库 | `E:\temp\wait_work\xcl`、`E:\temp\wait_work\VIctory` |
| Python（脚本用） | `C:\Users\xhqss\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe` |
| GPU/驱动 | AMD RX 7800 XT（gfx1100），amdvlk 32.0.32015.7029；Vulkan SDK `E:\VulkanSDK\1.4.363.1` |
| 代理（只在需要联网时逐命令传） | `-c http.proxy=http://127.0.0.1:7897`（**不要改任何全局 git 配置**） |

**规则**：可以本地提交，**绝不 push**。上游 `origin` = `https://github.com/ubiquitarianism/leonida`。

---

## 2. 构建与运行

```powershell
# 构建（必须先杀进程，否则 LNK1168）
cd E:\temp\wait_work\Leonida
Get-Process gta6rt -ErrorAction SilentlyContinue | Stop-Process -Force
cmd /c tmp\build-gta6rt.cmd gta6rt

# 默认跑法（稳定黑屏，但无错误；用于回归）
powershell -NoProfile -ExecutionPolicy Bypass -File tmp\run-probe.ps1 -Tag base -Seconds 30

# 带开关（run-probe 会自动给每个名字加 LEONIDA_ 前缀，不要再手写 LEONIDA_）
powershell -NoProfile -ExecutionPolicy Bypass -File tmp\run-probe.ps1 -Tag probe -Seconds 30 `
  -Set 'XS_TABLE_SCRATCH=1,XS_TABLE_SANITIZE=1,XS_DESC_SYNC=1,DEVICE_FAULT=1,CAPTURE=E:\temp\wait_work\Leonida\tmp\cap,CAPTURE_LIMIT=3'
```

`run-probe.ps1` 自动设置的基线环境：`LEONIDA_GUEST_TLS_SLOT0=1`、`LEONIDA_SHADERS=<语料>`、
`LEONIDA_D3D12=vulkan`、`LEONIDA_SKIP_ASSERTS=1`、`LEONIDA_SUBMIT_35=1`、`VK_LOADER_LAYERS_DISABLE=~implicit~`。
日志写到 `tmp\<tag>-<hhmmss>.log`（stderr）和 `.out`（stdout），带时间戳。

看帧是否有内容：

```powershell
$py='C:\Users\xhqss\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
Get-ChildItem tmp\cap\*.ppm | ForEach-Object { & $py tmp\ppm_stats.py $_.FullName }
# 现在是 min=0 max=0 non-zero 0/15770652（全黑）
```

**回归基线**：默认跑法 30 s 应有 ~1500+ 次 `PresentX call`、**0 次 device fault**；`ctest` 有 6 个既有失败
（`run_module_names`、`d3d12_com`、`d3d12_vulkan_device`、`d3d12_depth_resolve`、`d3d12_pipeline_library_host`、
`dxgi_swapchain`），不要把新失败混进来。

---

## 3. 架构（理解这条才能继续）

标题的调用链是：`game_scarlett_master.exe` → **d3d12_xs shim**（把 Xbox/console ABI 翻译成 PC D3D12 调用，
`runtime/shims/d3d12_xs_device.cpp` + `d3d12_xs.cpp`）→ **Leonida 的 D3D12→Vulkan 层**
（`runtime/shims/d3d12_vulkan_device.cpp` + `runtime/src/*`）→ amdvlk。

两层各自的关键约定：

1. **层（Vulkan 侧）的 GPU 地址模型**：一整块 host-visible 的 arena（默认 256 MiB，`LEONIDA_ARENA_MB` 可调，
   最大 4095 且受 `maxBufferSize`/`maxMemoryAllocationSize` 限制，本机约 2 GiB）。层的资源 GPU VA =
   `heap_base + arena 偏移`；翻译后的着色器里所有内存访问都写成 `heap_base + console_VA`，所以
   **console VA 必须落在 arena 内、且数据必须在 arena 的那个偏移上**。
2. **层（Vulkan 侧）的描述符契约**：`runtime/src/descriptor_table.h` —— 着色器把从
   `heap_base + 表VA` 读到的 **8 个 dword 描述符字节**做 FNV-1a 哈希，去 set 0 binding 2 的哈希表换 bindless
   索引。**所以表里的字节必须与层在 `Create*View` 时写进 heap 的字节逐字节相同**，否则全走 fallback 0。
3. **shim 的地址翻译**：`shader_address()` / `upload_translate()` / `guest_translate()`（`d3d12_xs_device.cpp`）。
   标题内存（host 地址 == console VA，见 `runtime/src/guest_memory.h`）会被拷进 4 MiB "window"（UPLOAD buffer）
   再把 window 的 console VA 交给着色器。

---

## 4. 已确认的事实链（按时间，附证据）

1. **present 链是好的**：`LEONIDA_PRESENT_TINT` 能把窗口涂成洋红；present 资源 id 与 draw #4 的 RTV id 一致。
2. **每帧 4 个 draw**（稳定阶段，`LEONIDA_LOG_REPLAY_DRAW` + `LOG_DRAW_PIPELINE` + `LOG_PRESENT`）：
   - #1/#2 `vs-01ac7810dcabd62a` + `ps-452212e59d983c3b` → 4K 中间纹理 A；
   - #3 `VS_Blit_6e5520d1_Wrapped` + `PS_BlitColour_6e5520d1_Wrapped` → A；
   - **#4** `)sga_dr_vs_pos_color_tex_c32788bc_Wrapped` + `default_ps_tex_c32788bc_Wrapped` → **送显纹理 P**。
3. **黑屏来自 draw #4 的位置**：把 #4 的 VS 最后一条 `OpStore %out_pos` 换成 gl_VertexIndex 全屏三角形后，
   帧变成 `min=0 max=255 non-zero 66.67%`（洋红铺满）⇒ draw→管线→RTV→present 全通，问题在 VS 自己算的位置。
4. **#4 的 VS 需要什么（静态读语料 `.spvasm` 得到，`docs/...black_frame...md` 第八轮）**：
   - 表地址 = `heap_base + ((s9<<32) | (s8 & ~3))`，即 s8 是表 console VA、s9 必须为 0；
   - 在表 **+0x00** 读一个 16 字节 V#（4×4 位置矩阵），在表 **+0x40** 读另一个 V#（顶点缓冲 +0x160）；
   - V# 字段解释 = **标准 gfx10**：`base = (dword1 & 0xffff)<<32 | (dword0 & ~3)`，
     `stride = dword1[29:16]`，`num_records = dword2`，然后按 `num_records*stride`（stride=0 用 num_records）
     做边界检查。
   ⇒ **"格式未知"这个前提是错的，格式就是 gfx10 V#。** 之前几轮"槽=32 字节 {A,B}、A/B 都不是 V#"的结论作废，
   因为那是从 `copy_committed(0xb94ef00)` 读到的**无关宿主内存**（`is_committed` 陷阱，见 §9）。
5. **表 VA 不在 descriptor heap 里**（第九/十轮，`LEONIDA_LOG_HEAPID`/`LOG_SLOTWRITE`/`XS_GPU_HANDLE`）：
   - 层只有 1 个 shader-visible heap（type 0，1,245,184 槽，GPU handle = arena 偏移 0xae10000，与
     `real_cpu_start − arena_host_base` 完全一致，没有 off-by-base）；
   - 整轮只观察到 **63 次**描述符写入，槽号都 ≤103；而标题 bind 的表 VA（0xb94ef00）换算成槽号是 **368504 起、
     每帧 +4096** ⇒ 那个槽从未被任何描述符 API 写过，`DescriptorRange` 命中只是**数值上恰好落在 handle 区间**；
   - 标题自己的 XMem heap 副本在这些槽上是**分配器残留**（全 0 或 −1.0f/1.0f 之类）。
   ⇒ **表 VA 是标题自己的内存（host 地址 == console VA），不是 heap 槽。**
6. **表的内容**：在 `0xb94ef00` 读到的是可解码的 gfx10 描述符；V# base 形如 `0x8cf584b4`、`0xb50516a0`
   （**已提交的标题宿主地址**），也混着 `0x80800000004`、`0x84400000002`、`0x1500ba4e...` 这类
   **既不在宿主内存内、也不在 arena 内**的 base（着色器 `heap_base + base` 读它们 = device fault）。
7. **层的描述符写入是正常的**：`LEONIDA_LOG_DESC_WRITE` 显示 T# 都写进 arena（`in arena 1`）；
   `XS_DESC_SYNC` 打印的层的 V# base 都是 arena 内小地址（`0x10000`、`0x80001b00`…），T# base 是 4 GiB 级
   的**哈希键**（不是地址，正常）。
8. **compute root table 之前完全没翻译**：console list slot 35 = `SetComputeRootDescriptorTable`
   （`docs/d3d12_command_objects.md` 对照表）。补上后**那条 fault 从 1 变 0**，确认它的来源就是 CS 读未翻译的表。
9. **每张表 ~75% 的槽位仍被判定为"base 超出 arena 的 V#"**（32 字节槽扫描，1547/2048 之类）⇒ 表的真实格式
   或"哪张表才是着色器读的"还没对准，见 §7。

---

## 5. 已排除的假设（别重复做）

| 假设 | 结论 |
| --- | --- |
| 表内容是半成品（帧间变化） | 90 s 采样 word 变化数 = 0，表第一帧后就稳定 |
| 表是 gfx10 16 字节 V# 数组 | 单表 256 项全部能翻译、0 清零（说明"像 V#"），但整表 75% 槽不可达 ⇒ 布局/来源仍未定 |
| 表在 descriptor heap 的某个槽 | 槽从未被写过；只是数值落在 handle 区间 |
| 层写描述符的位置错了 | 写入都在 arena，getter/偏移全一致 |
| 指针链（entry → *(entry) → 数据） | 跟随一层后仍黑，且 `is_committed` 读到的"指针"是无关内存 |
| 表条目需要 rewrite base 才能用 | `XS_TABLE_VBASE` 全表重写 256 项时无 fault，但 256×4 MiB 窗口 = 1 GB > arena，卡死 |
| 只改字形/窗口大小能过 | 见 §6 的帧率结论 |
| 上游已修好同题 | 上游 `docs/boot_leads.md` **BL-4 就是同题且仍是开放状态**（见 §10） |

---

## 6. 当前状态与三个卡点

**能稳定复现的最"正常"跑法**（默认，无开关）：30 s 内 1500+ presents、0 device fault、画面全黑。

**带实验开关**（`XS_TABLE_SCRATCH=1,XS_TABLE_SANITIZE=1,XS_DESC_SYNC=1`）：

- 表能被着色器读到（scratch 生效：`0xb94ef00 -> slot 0 at 0x30bc0000`，固定 8 MiB mapped UPLOAD buffer，
  128 槽 × 64 KiB）；
- 但 **30 s 只有 1 帧**，然后标题进入 `list wait ... answered, no GPU wait` 循环直到退出；
- 且有一个**固定 device fault**：`console VA 0xd1e7dc27000`（九次运行复现）。

三个待解问题（优先级从高到低）：

1. **fault `0xd1e7dc27000` 的来源**。它**不经过 `shader_address`**（`LEONIDA_LOG_BADVA=1` 一个都没打），
   所以只能来自：root descriptor 系列（console list 41-46）、VBV/IBV（24-27）、SO（49）、
   `ExecuteIndirect` 参数 buffer，或层内部自己算的地址（`write_image_descriptor` 的
   `buffer.address = GetGPUVirtualAddress() + byte_off`）。
2. **表内容与 gfx10 对不上（75% 槽不可达）**。要么这些表不是着色器真正读的那张，要么标题写表时用的不是
   gfx10 编码（需要确认标题是否真的把层写入的字节拷进表里）。
3. **帧率**。一帧几十个表绑定，每绑定一次拷贝 64 KiB + 扫描；scratch 已经去掉"每 VA 一个窗口"的大头，
   但仍只出 1 帧，说明还有别的重活（或上面两个问题导致标题停住）。

---

## 7. 下一轮的具体步骤（按优先级）

### 步骤 A：定位 `0xd1e7dc27000`

在 `runtime/shims/d3d12_xs_device.cpp` 里，把所有**最终会进着色器**的地址出口统一加一个检查
（类似 `LEONIDA_LOG_BADVA`，但覆盖所有出口）：root descriptor setter（list slot 36 之外的那些）、
`IASetVertexBuffers`/`IASetIndexBuffer`、`SOSetTargets`、`ExecuteIndirect` 的 argument buffer、
`WriteBufferImmediate`。判据：结果 `>= leonida::arena_host_size` 就打印 slot + 值 +
`VirtualQuery` 状态 + 是否 committed。

同时在层侧 `write_image_descriptor`（`d3d12_vulkan_device.cpp` ~8375 行）和 buffer SRV 路径
（`CreateShaderResourceView` ~6545 行）里，对 `buffer.address` 做同样的检查并打印。

拿到来源后：如果它是标题 VA（committed）→ 用 `guest_translate` 换成 window 地址（**注意 T# 是哈希键，不能只改
地址**，见 §8 的坑）；如果不是 committed → 清零/跳过。

### 步骤 B：确认"表里的字节 == 层写入的字节"

1. 开 `LEONIDA_XS_DESC_SYNC=1`（层写入后拷回标题 CPU handle 内存）。
2. 开 `LEONIDA_LOG_TABLE3=1`（对表 VA 打四份：arena / 标题宿主内存同数值 / range 两侧）。
3. 在同一帧里对比：`Create*View` 写入的 32 字节（`LEONIDA_LOG_DESC_WRITE`）是否出现在表 VA 的某个偏移上。
   - 出现 ⇒ 标题会自己拷贝，`DESC_SYNC` 方向正确，继续调表格式；
   - 不出现 ⇒ 标题有自己的描述符编码，需要把它当"console descriptor 格式"重新解（`xcl` 的
     `crates/xcl/src/hle/d3d12x/console_heap.rs` 是唯一权威参考：**console 槽 = 32 字节 T#/V#，16 位 sampler，
     表就是 32 位 GPU 地址**）。
4. 用 `XS_TABLE_SCRATCH` 时表被拷到 scratch；确认拷贝前后的字节一致（第 3 步的对比在 scratch 内存里做）。

### 步骤 C：解决帧率/停住

- 开 `LEONIDA_LOG_REPLAY_DRAW=1` 看最后提交的 draw/命令，确认是"GPU 慢"还是"标题在等"。
- 检查 scratch 槽位是否在一帧内被覆盖（128 槽，一帧绑定数应远小于此；若不够，加到 256 槽/16 MiB）。
- 若最终确认需要"大窗口"方案：参考 §10 的 xcl 做法（少量 4 GiB raw view 窗口），但注意本机
  `maxMemoryAllocationSize` ≈ 2 GiB，且 `LEONIDA_ARENA_MB` 上限 4095。

### 步骤 D：验证渲染是否恢复

- 每次实验后都跑 `tmp\ppm_stats.py` 看帧统计；只要 `max > 0` 就说明有内容了。
- 回归检查：默认跑法 30 s 必须仍是 1500+ presents、0 fault。

---

## 8. 踩过的坑（务必先读）

1. **改任何 header 之后必须 touch 所有 .cpp 再构建**，否则 ninja 只重编一个 TU，结构体布局不一致 →
   amdvlk 里 3–5 秒后随机 `c0000005`（症状是"游戏 3 秒退出"）。命令：
   ```powershell
   Get-ChildItem runtime\shims,runtime\src -Filter *.cpp | % { $_.LastWriteTime = Get-Date }
   cmd /c tmp\build-gta6rt.cmd gta6rt
   ```
2. **`is_committed(addr)` 会骗人**：宿主进程里 0xb94ef00 这种低地址很可能已被别的映射占用；
   "读到了看起来像描述符的字节"不等于"那是标题写的表"。判断一个 VA 的意义前，先看它是否在
   `DescriptorRange` / XMem 记录里。**前四轮的"表内容"结论全部因此作废**。
3. **console 槽是 32 字节**：T# 占满 8 个 dword，V# 只用前 4 个。按 16 字节步进扫描会把 T# 后半当 V#，
   清零就等于毁掉 bindless 哈希键。
4. **T# 的 base 是哈希键，不是地址**：层的 T# base 是 4 GiB 级的值，着色器只哈希不寻址；不要"翻译"或清零它。
5. **device lost 之后 `ExecuteCommandLists` 一律报 `unsupported`**：那是队列拒绝，不是命令不支持。
   看 `present failed (VkResult -4)` 或用 `LEONIDA_DEVICE_FAULT=1` 的 fault address。
6. **`LEONIDA_CAPTURE` 的帧是 PPM**（3076×1709），用 `tmp\ppm_stats.py` 统计。
7. **语料实验必须还原**：改过的 `.spv` 要有备份，还原后按 sha256 核对。当前
   `default_ps_tex_c32788bc_Wrapped.spv` 必须是 `E231E529E80341216949C62E8127163AD3F7EFDBC8DA29E77247114F41A0BAA3`。
8. **不要用 PowerShell 读写项目文件**（编码/BOM 问题），用内置文件工具。
9. 构建前必须 `Stop-Process gta6rt`（LNK1168）。
10. `LEONIDA_LOG_TABLE` / `LOG_TABLE3` / `LOG_VSHARP` 输出很啰嗦，跑长实验时别一起开。

---

## 9. 代码改动地图（本地相对上游的新增，全部 opt-in、默认关）

`runtime/shims/d3d12_xs_device.cpp`：

| 名字 | 作用 |
| --- | --- |
| `LEONIDA_LOG_HEAPID` | 每个 descriptor heap 的 id/type/槽数/XMem 地址/arena 偏移/GPU handle + 前 6 个 heap 两侧 slot 0 字节 |
| `LEONIDA_LOG_SLOTWRITE` | 每次 `Create*View`/`CopyDescriptors` 写哪个 heap 哪个槽；每 heap 的写次数与最大槽号 |
| `LEONIDA_LOG_TABLE3` | 表 VA 三路对照 dump（arena / 标题宿主内存 / range 两侧）+ 前 8 槽 V# 解码 |
| `LEONIDA_LOG_TABLE_STABLE` | 同一表 VA 连续采样，打印变化与稳定快照 |
| `LEONIDA_LOG_VSHARP` | 在任意 4 字节偏移上给 16 字节窗口打分，找 V# |
| `LEONIDA_XS_GPU_HANDLE` | 把 shader-visible heap 的 GPU handle 区间注册进 `DescriptorRange` |
| `LEONIDA_XS_TABLE_VA` | 表 VA → 4 MiB window（旧方案，慢） |
| `LEONIDA_XS_TABLE_SCRATCH` | **表 VA → 固定 8 MiB scratch 的第 k 个 64 KiB 槽（新方案）** |
| `LEONIDA_XS_TABLE_SANITIZE` | 表窗口里 base ≥ arena 的 V# 清零（32 字节槽） |
| `LEONIDA_XS_DESC_SYNC` | 层写的 32 字节描述符拷回标题 CPU handle 内存 + 打印 base/committed |
| `LEONIDA_XS_CPU_HANDLE_ARENA` | 把 heap 的 CPU handle 指进 arena（会崩，未解决，默认关） |
| `LEONIDA_XS_TABLE_FIX` / `_REWRITE` / `_INDIRECT` / `_VBASE` | 历史实验，结论见 §5，**不要再开** |
| `LEONIDA_LOG_BADVA` | `shader_address` 无法翻译、只能原样交出的地址打印出来 |

`runtime/shims/d3d12_xs.cpp`：`configure_virtual_memory` 现在填 `[0x10000, 0x7ffffffeffff]`
（console 的 GPU+CPU 共用地址空间 = 进程 application 地址区间；上游 `028001c82` 实测同结论）。

`runtime/shims/d3d12_xs_device.cpp` 里还补了 **list slot 35 → `SetComputeRootDescriptorTable`** 的翻译。

---

## 10. 上游与兄弟库状态（2026-11-25 01:00）

- 本地 `main` 领先 120、落后 1343（`origin/main = 82a2aabcc`）。**不要 merge**（两边在 `d3d12_xs_device.cpp`
  上已大幅分叉）。
- **上游 `docs/boot_leads.md` 的 BL-4 与当前问题完全同题且仍是开放状态**：
  "a console descriptor word may be a 32-bit GPU address rather than a heap offset … translate a word to the buffer
  device address … the way root-descriptor addresses already go through the arena"。**所以这是一条真正的开放
  问题，不是漏了某个补丁。**
- 上游有用的单点：`028001c82`（VM VA 区间，已移植）；其余（slot 81/86/108、`CreateCommittedResource3`、
  `rdna2spirv` 接受 vcc 基址等）与当前症状关系不直接。
- **xcl 新提交与本题高度相关**：`fe7d70b`（memory windows from the descriptor heap：把 console 地址空间做成
  shader-visible heap 头部的几个 **4 GiB raw UAV 窗口**，着色器一次 `ResourceDescriptorHeap[...]` 访问任意窗口）、
  `deb4b92`（窗口视图最多 **4 GiB − 32 B**，Win11 23H2 的 D3D12 会拒绝更大的并移除设备）。
  ⇒ 如果 scratch 方案不够，正解方向是"少量巨大窗口"，不是"每个 VA 一个 4 MiB 拷贝"。
- **VIctory** 新提交都是 shader 翻译（waterfall、per-lane phi、cmp-lg），与黑屏无直接关系。

---

## 11. 提交与规则

- 最新提交：`f87a51099`（scratch 方案）、`6981d628f`（compute 表 + sanitize）、`30a9772e1`（VA 区间 + desc sync）、
  `bb6c9fe54`、`47285f4c8`、`66c2c7a8e` 等（全部**只在本地，未推送**）。
- 用户长期授权：`E:\temp\wait_work` 内可自由改、可本地 commit；**唯一要求：游戏能跑起来**；**不要 push**。
- 删文件前需针对具体目标确认（用户已授权清理过 VkCache 与 5 个测试垃圾文件，新的删除仍要问）。
- 网络：只在 `git fetch` 等必要场景联网，逐命令带代理，不改全局配置；境外资源连续失败 3 次就停并报告。

---

## 12. 文档索引

- 主文档（本轮全部证据、逐轮记录、中文）：
  [docs/2026-11-24_black_frame_and_descriptor_tables.md](2026-11-24_black_frame_and_descriptor_tables.md)
  —— 第一～十三轮，包含每个探针的原始日志片段、已排除的假设、坑。
- 更早的 bring-up：
  [docs/2026-11-24_overnight_bringup.md](2026-11-24_overnight_bringup.md)、
  [docs/2026-11-24_upstream_merge_and_white_window.md](2026-11-24_upstream_merge_and_white_window.md)。
- 层与翻译器的设计：`docs/design.md`、`docs/d3d12_vulkan.md`（§3.3 GPU 地址、§5.1 描述符哈希表）、
  `docs/d3d12_command_objects.md`（console↔PC slot 对照）、`docs/shader_library.md`（语料流程）。
- 语料与 shader 工具：`tools/`、`tools/rdna2spirv.py`、`runtime/src/descriptor_table.h`。

---

## 13. 一句话给接手者

**别再猜表格式了**：表的 VA 是标题自己的内存，着色器要的是"`heap_base + 表VA` 处能被 GPU 读到的 gfx10 描述符
字节，且这些字节要与层 bindless 哈希表里的 key 一致"。现在缺的是三件事按顺序：
① 找出 `0xd1e7dc27000` 是哪个 API 给的地址；② 确认标题表里的字节与层写入的字节是否一致；③ 把表固定在一个
GPU 可读的小池子里（`XS_TABLE_SCRATCH` 已做，差最后一公里）。把这三件做完，再跑 `ppm_stats.py` 看 `max` 是否
不再为 0。
