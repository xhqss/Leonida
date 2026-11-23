# 2026-11-23 20:00 → 2026-11-24 02:18 夜间攻坚记录

零售版 `game_scarlett_master.exe`（GTA VI Xbox，1.0.2.1）在本机 Windows 上经 D3D12→Vulkan 层启动的
一次性记录。范围是这一晚实际做的事、测到的数、以及下一次接手时应该从哪里继续。

不是上游 PR 的说明文档，只是本次会话的工程日志；结论都能在本文引用的日志、trace 或提交里复核。

---

## 1. 一句话总结

一晚把启动链从"进入 guest 后很快停住"推进到两处可复现的硬停点：

- **默认（不跳过 assert）**：`int3` 在 `game_scarlett_master.exe+0x344196`，进程启动后 **5.58 s**。
- **跳过第一个 assert（`LEONIDA_SKIP_ASSERTS=1`）**：跑到 **11.3–11.5 s**，改一条 GPU 页表项时
  写进一个"只保留、从未提交"的地址范围而 AV（`+0x25a087d`，见 9.2）。

并且确认了此前 300 秒级"卡死"的真实原因：fence 槽位 12 返回 `E_NOTIMPL`，标题进自旋，最后由它自己的
30 秒看门狗写给地址 0（`+0x2215c35`）。合并上游 PR #7290 后该自旋消失，单次启动从 309 s 掉到 11.3 s。

凌晨之后又往前推了一步：控制台原语拓扑 19 的 Blit PSO 现在按三角形列表建成，语料里缺的 Blit VS 已翻译
并入（26,555 → 26,556），`no translated vs` 归零。剩下的停点就是上面那条页表写入（9.2、9.3）。

**03:00–04:30 这一段又把硬停点换了三次**（§9.4）：

1. `LEONIDA_DUMP_SHADERS` 现在连 **DXBC 容器**一起写（`<stage>-<sha>.dxbc`），于是 dump 出来的程序可以
   真正翻译了——容器里有签名、PSV0 和 FSSM 头部（`COMPUTE_PGM_RSRC2`、线程组大小）。用一个缺译文的 PS
   验证过：语料 26,556 → **26,557**，那条 `no translated ps` 消失，它要的 PSO 建了出来。
2. 设备槽位 81 的两种答法都试了（§9.2b 的假设）：**都不是页表 AV 的原因**，AV 发生在工作线程上，
   与主线程的槽位 81 无关。这条假设可以划掉。
3. 页表写入 AV 的真面目查清了（`LEONIDA_PROBE_ARRAY` 探针）：那确实是一张 8 字节 PTE 的页表，
   数组里是 PTE 的字节偏移；但数组**只读到前 ~8 KB 就断了**，后面是堆垃圾，标题又不看读返回长度，
   于是拿垃圾偏移去写 PTE。README 级的坑，但根因在流读取。
4. **给 DStorage 补上 zlib 解压**（`src/zlib_inflate.h` 从 `shader_library.h` 拆出去复用）之后，
   那条 AV **消失了**：启动推进到 **16.4 s**，死在音频初始化之后的 `0xC0000409`（fastfail）。
   也就是说页表 AV 是"流数据没解压出来 → 布局表是垃圾"的下游症状，不是页表本身的问题。
5. **音频的 9 个导入从"返回 0"改成"写出参"**（新的 `runtime/shims/audio.cpp`）：窗口 `Americas`
   起来之后**不再死**——实测一次运行活过 8 分钟、`Responding`、工作集 19.9 GB，一直在建 FSPX compute PSO
   （每条都因为语料里没有译文而失败，标题不因此退出）。**这是本晚最好的状态：从"15 秒必死"到"能一直活着"**；
   画面还停在白色（只有一次 `PresentX ready`，没有第二帧）。

---

## 2. 起点与终点

| 项目 | 起点（11-23 20:00 前后） | 终点（11-24 02:18） |
| --- | --- | --- |
| 上游基线 | `561b65a` | `a779d6411`（384 提交，合并提交 `b7b7e8151`） |
| 本分支 | 刚从 `561b65a` 拉起 | `5bf5ad4f0`，领先 `origin/main` **102** 提交，落后 0，全是本地提交，未推送 |
| 构建 | 部分目标 | 全量 `all` = BUILD-OK（466 个构建步骤） |
| 测试 | 未跑 | 串行 ctest：**98% 通过，268 项里 5 项失败**，237.95 s |
| 启动 | 内存表面/描述符堆一带停住 | 5.58 s（默认）/ 11.3 s（跳过 1 个 assert），停在一条 GPU 页表项的写入 |
| 管线缓存 | 无可用缓存（启动时现场编译） | `pipeline_cache.bin` 760,673,388 B，放在 `gta6rt.exe` 旁边 |
| 着色器语料 | 26,503 条 | 26,556 条（52 个 `XboxBvhBuilder.dll` 的 compute + 1 个 Blit VS） |

---

## 3. 时间线（按 trace 实测）

每次运行都带 `--trace tmp/trace-N.jsonl`，trace 里的 `t` 是进程启动后的秒数。
下表是从 `tmp/trace-*.jsonl` 直接汇总的，不是回忆：

| 时刻 | trace | 停点 | 说明 |
| --- | --- | --- | --- |
| 23:13 | trace-1 | 0.91 s `unimplemented_call` | 合并 `561b65a` 后第一次能进到 guest 代码 |
| 23:17 | trace-2 | 0.90 s `int3 @+0x1e1d01b` | 第一个 guest assert |
| 23:19–23:29 | trace-3..11 | ~1.0 s `AV @+0x104075` | 静态 TLS 访问，guest 的 TLS 槽位和宿主冲突 |
| 23:23 | trace-5 / 7 | ~1.0 s `tls_callback` | 用 TLS 回调路径反推槽位编号 |
| 23:33–23:36 | trace-cdb..cdb4 | 1.1–1.4 s `xmem_virtual_alloc` | 接调试器看真实调用参数，不再靠猜 |
| 23:39–23:52 | trace-12..25 | ~1.0–1.5 s | 宿主侧 AV、堆破坏 `0xc0000374`、`XAccessibilityImpl` 缺槽位 |
| 00:35 | trace-26 / 27 | 1.0–2.0 s | `LEONIDA_GUEST_TLS_SLOT0` + `XErrorReport` 落地后越过 TLS |
| 00:57 | trace-28 / 29 | 2.1 s | 控制台内存表面（`XMemAllocatePhysicalPages` 等） |
| 01:10–01:34 | trace-30..50 | 2.2–2.35 s | 描述符堆 / 设备槽位 25、26、127 拉锯（最长的一段） |
| 01:35–01:38 | trace-51..54 | 3.9–4.0 s `e06d7363` | 标题抛 C++ 异常（`XErrorReport` 相关路径） |
| 01:40–01:42 | trace-55..57 | 32.2 s `AV @+0x2215c35` | 看门狗写给地址 0，写值 0 |
| 01:43–01:55 | trace-59..74 | 5.1–6.4 s `int3 @+0x344196` | 描述符句柄 assert，之后靠槽位 73/74/41 往前 |
| 02:01 | trace-75 | **309.2 s** `AV @+0x2215c35` | fence 槽位 12 返 `E_NOTIMPL` → 自旋 → 看门狗 |
| 02:03 | trace-77 | **11.27 s** `AV read @+0x25a087d` | #7290 修好 fence 后的新停点（拓扑 19） |
| 02:21 | trace-78 | **5.58 s** `int3 @+0x344196` | 复核：默认不跳过 assert 时的稳定停点 |
| 02:44 | trace-86..89 | **11.6 s** `AV read @+0x25a087d` | 加了设备槽位 81 的参数日志；同一处页表写入 |
| 03:13 | play.log | 窗口 + `PresentX ready`，然后 `no translated ps` | 缺译文的 PS（`452212e5…`），PSO 建不出来 |
| 03:2x | trace-90 | 6.0 s `int3 @+0x344196` | 不带 `LEONIDA_SKIP_ASSERTS` 的默认停点（复现） |
| 03:3x | trace-91 | 12.5 s `AV @+0x25a087d` | 把 PS 并进语料后，同一个 AV（说明与 PS 无关） |
| 03:4x | trace-92..95 | 12–15 s `AV @+0x25a087d` | `LEONIDA_PROBE_ARRAY` 探针：页表、数组、流状态 |
| 03:5x | trace-96 | 13.0 s 同一 AV | `LEONIDA_SLOT81=zero`：AV 不变 → 槽位 81 不是原因 |
| 04:0x | trace-97 | **16.4 s `0xC0000409`** | DStorage zlib 上线后 AV 消失，死在音频初始化之后 |
| 04:1x | trace-98..100 | 15.2×2 s `0xC0000005` 写 `0x20` | 音频的 out 参数没写：`memset([ctx+0x38]=0)`、`call *0` |
| 04:2x | trace-101 / run-audio3.log | **活过 8 分钟** | 音频桩落地：窗口 `Americas` 在前台且 `Responding`，19.9 GB，一直在建 FSPX compute PSO |

要点：**"窗口拉起后未响应"的那一次就是 309 s 那次**（见第 8 节）。

---

## 4. 上游同步

- `git merge origin/main`（`a779d6411`，384 提交）→ 合并提交 `b7b7e8151`。
- 冲突按上游取舍；本地保留项：`LEONIDA_GUEST_TLS_SLOT0` 运行时开关、`XErrorReport` 按名导出。
- 之后 `origin/main` 又前进到 `9b60c92c9`，**尚未合并**（用户 11-24 02:18 确认上游暂时没更新，本轮不拉取）。

---

## 5. 按上游 PR 移植的部分（全部本地 merge，未推送）

这些是照上游 issue #7154（零售版启动日志）的梯子挑的，编号即上游 PR 号。

| PR | 内容 | 落地提交 |
| --- | --- | --- |
| #4348 | 控制台 DirectStorage | `4bf6b103c` |
| #2490 | 控制台 ABI 命令列表 | `5ddae14c0` |
| #7021 | 描述符堆走 XMem 分配钩子 + CPU 句柄翻译 | `b24df8754` |
| #7052 | 设备槽位 127 = `CreateCommittedResource` | `8914f99df` |
| #7110 | 控制台 tile 尺寸、槽位 25 里清洗 Xbox 描述 | `ceee795dd` |
| #7240 | 渲染目标按控制台 tile 尺寸租内存 | `e49e34da2` |
| #7066 | 槽位 70 由代理自己处理 | `2f3817809` |
| #7163 | 控制台间接参数 type 105 = dispatch | `1c1dd946d` |
| #7115 | 槽位 61 = `CreateSamplerX` | `0436cec3b` |
| #7201 | 槽位 86 / 101 / 108 → PC `Device2/5/8` | `f881087cf` |
| #7755 | A8_UNORM 支持 1D / 3D | `1e90eb871` |
| #7593 | A8_UNORM 渲染目标 → `VK_FORMAT_A8_UNORM_KHR` | `749c362d6` |
| #7204 | `LEONIDA_SKIP_ASSERTS=N` 跳过前 N 个 guest `int3` | `838d5b228` |
| #7290 | fence 槽位 12 返回"活着的已完成值"地址 | `bf9a0b2e0` |
| #6891 | `XGCreateTextureComputer` 等按 trace 的 `E_NOTIMPL` | `120d042a0`（按文件拷贝应用） |

中间有一步**合并后回退**：`#7235`（槽位 127 通过 XMem 钩子取资源内存）合进来（`4533c5f6f`）后，
标题自己的 bump allocator 断言，因此回退（`07b4c853d`），改用 `#7240` 的租约方案。
回退原因记在这里，避免下次又踩一遍。

---

## 6. 自己写的部分（不是上游 PR）

| 提交 | 内容 |
| --- | --- |
| `637bd3b01` | 让被映射的镜像占用 module TLS index 0（`LEONIDA_GUEST_TLS_SLOT0=1`） |
| `3d9a9acef` | `xgameruntime` 按名提供 `XErrorReport` |
| `1d5936bad` | 提供零售 exe 需要的控制台内存表面 |
| `44752ca17` | 只保留一个 `configure_virtual_memory`（实测的 4 参数版本） |
| `9ecee2f38` | 让启动继续走过接下来那批设备调用 |
| `23e499f0a` | 设备表扩到 192 槽 + 槽位 133 的 placed-resource 垫片 |
| `374e5b891` | 槽位 48 的描述本来就是 PC 形状时直接透传 |
| `f4504f8f3` | 越过描述符句柄 assert 与槽位 25，并给后续调用加探针 |
| `ee9e208d0` | 越过命令签名、槽位 74、窗口类 |
| `1d8d1d355` / `9c8522d22` | 槽位 70 的原始 buffer 视图（取 buffer 引用加锁、宽度够放下整个 range） |
| `3ace9869d` | 槽位 56 在调用方不给 per-resource 数组时也给同一个控制台总量 |
| `359390833` | 控制台渲染目标尺寸：按资源的对齐、槽位 56 偏移、完整 mip 链、向上取整的 mip 尺寸、B5G5R5A1、饱和求和 |
| `3c95a4a95` | `QI` 与 `CreateCommandListX` 里映射控制台 list 的 IID |
| `5be2fd287` | 直接调设备代理的调用方默认按 PC 形状的 list 处理 |
| `ee93d8ce2` 等 | `run_gpu_tests --assembler/--no-validate/--disasm-dir` |
| `6b21ec996` / `5bf5ad4f0` | 测试跟随新行为：mesh 签名建成 compute dispatch；恢复代理命中计数 |

实测下来这些槽位的形状（也是本地代码里写死的依据）：

- **25** `GetCustomHeapProperties`、**26** `GetResourceAllocationInfo`：MSVC 走隐藏返回缓冲，调用方传的缓冲必须写满。
- **41** `CreateCommandSignature`：type 105 和 `DISPATCH_MESH` 都建成 `DISPATCH`。
- **48** `CreateCommandQueueX`：`Flags` 里带控制台位，清洗成 PC 的 `DISABLE_GPU_TIMEOUT`，`NodeMask` 归 0。
- **61** `CreateSamplerX`、**70** `CreatePlacedResource1`、**73** `CreateCommandListX`、**74** 直接 `S_OK`。
- **78/79/80** 帧事件、**86/101/108** 路由到 PC `Device2/5/8`、**127** committed resource、**133** placed resource。

---

## 7. 着色器语料与管线缓存

### 7.1 语料

`XboxBvhBuilder.dll` 里嵌的 52 个 compute shader **不在 `shaders.rpf` 里**，是要单独抠的。
整条链（缺一步都出不来）：

```text
tools/pe_shader_containers.py <dll> --extract DIR
tools/fssm_unpack.py DIR
  -> 把 .s 放进 DIR/disasm（翻译器会在上两级目录找 index.json）
tools/shader_disasm.py
tools/rdna2spirv.py ... --shard 0 / 1   （两个分片各自出 .spv）
tools/rdna2spirv.py ... --merge
  -> 把 .spv/.spvasm 与 merge 索引条目并进语料
```

结果是语料 `index.json` 从 **26,503** 涨到 **26,555** 条，`.spv` 文件数同为 26,555。
`shaders\spirv-561b65a` 就是运行时用 `LEONIDA_SHADERS` 指过去的目录。

之前 `no translated compute shader for FSSM sha256 b5316b2e…` 就是这个坑，补完之后启动直接推到 32 秒
（当时的停点是 fence 自旋，不是缺 shader）。

### 7.1b 运行时抓缺失的 shader（本轮新增）

不是所有缺译文都能靠 `shaders.rpf` 反查——有些程序只出现在标题运行时的请求里（比如那个 Blit VS）。
现在运行时可以直接把它们写出来：

```powershell
$env:LEONIDA_DUMP_SHADERS='E:\temp\wait_work\Leonida\tmp\missing-shaders'
```

命中"语料里没有"的分支时，写两份文件：

- `<stage>-<sha256 前 16 位>.fssm`：运行时用来算 `fssm_sha256` 的那段程序字节；
- `<stage>-<sha256 前 16 位>.txt`：阶段、完整 sha256、字节数，以及当时的管线形状
  （输入布局每个元素、MRT 数量与格式、DSV、采样数、拓扑类型）——VS 要用哪一份变体就是靠这个判断。

此后一条命令走完反汇编与翻译（`shader_disasm.py` 只吃 `fssm/` 里的文件，翻译器要上两级目录的 `index.json`）：

```powershell
# 1. 只留 .fssm，并造出 carve 形状的 index.json（stage_of() 从这里读阶段）
& .\.venv\Scripts\python.exe -I tmp\write_dump_index.py tmp\missing-shaders
# 2. 反汇编（llvm-mc 在 WSL 里，Windows 官方 LLVM 没有 AMDGPU target）
$env:LLVM_MC='wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc'
& .\.venv\Scripts\python.exe -I tools\shader_disasm.py tmp\missing-shaders\fssm --out tmp\missing-shaders\asm
# 3. 翻译（spirv-val/spirv-as 来自 Vulkan SDK，要在 PATH 上）
$env:PATH="E:\VulkanSDK\1.4.363.1\Bin;$env:PATH"
& .\.venv\Scripts\python.exe -I tools\rdna2spirv.py tmp\missing-shaders\asm --out tmp\missing-shaders\spirv
# 4. 并进语料（键已存在且 sha256 不同就停，不覆盖）
& .\.venv\Scripts\python.exe -I tmp\merge_shaders_into_corpus.py `
    'E:\temp\wait_work\leonida-work\shaders\spirv-561b65a' tmp\missing-shaders\spirv
```

本轮实测：40 个程序里 24 个能干净反汇编，其中 **1 个直接翻译成功**（Blit VS）；另外 23 个都是 compute
（19 个报 `CS group size`、4 个报 `CS thread-group-size SGPR`），原因是翻译器要 `COMPUTE_PGM_RSRC2` 和
线程组大小（FSSM 头部 0x138..0x144），而 dump 出来的是"程序"、不含头部；剩下 8 个反汇编失败
（`no s_endpgm`）和 8 个 `scan failed` 的是 FSPX kind-6，不是 FSSM 布局。

### 7.2 管线缓存

- 分片目录：`tmp/cache-shards-20261124`，12 个分片（`shard-0..11.bin`，每个 50–80 MB）。
- **10 个跑完**；`shard-7` 和 `shard-9` 各自卡死在同一个模块上，8,703 s 没有任何输出后被取消：
  - `indirect_diffuse_lighting_CS_ScreenProbes_ActivateProbesTarget_534f271f_Wrapped`（shard-7）
  - `indirect_diffuse_lighting_CS_ScreenProbes_ActivateRays_d60186e6_Wrapped`（shard-9）
  - 两个 `.log` 是 0 字节、`.err` 只有 "still compiling … after 8703s" 的重复行，可以确认不是我的脚本崩了，是驱动/翻译器在这一条上不收敛。
- 合并命令：`vk_cache_merge.exe <target> <small> <shards...>`，产物
  `runtime/build-561b65a/pipeline_cache.bin` = **760,673,388 B**（01:42 生成，早于两个分片被取消）。
- 运行时从 `gta6rt.exe` 同目录加载 `pipeline_cache.bin`。

---

## 8. 观察到的现象：窗口能拉起，随后"未响应"，标题是 Americas

用户观察到最长的一次是：**窗口起来了，标题写着 `Americas`，然后窗口未响应。**

对得上的是 02:01 那次（trace-75）：

- 单次进程活了 **309.16 s**；
- 日志里最后一次渲染相关的动作是 `fence slot 12 … returning E_NOTIMPL`；
- 309.16 s 时在 `game_scarlett_master.exe+0x2215c35` 写地址 **`0x0`** 触发 `0xc0000005`。

即：窗口（标题自己的 `Americas` 窗口，经 `RegisterClassExA`/`CreateWindowEx` 转发到宿主 user32 建的）确实建出来了，
之后卡在fence 等待上——槽位 12 当时没有实现，标题的每帧等待永远拿不到"已完成值"，于是它自己的 30 秒看门狗
在写 `mov dword ptr [0], 0` 时崩掉。**"未响应"不是我们的死锁，是 fence 语义没实现。**
合并 #7290 后同一路径 11.27 s 就继续往前走了，自旋消失。

顺带：`XAccessibilityImpl` 槽位 9 这类"离线不可用"的 `E_FAIL` 是无害的，标题会忽略。

---

## 9. 当前停点（下一步的入口）

### 9.1 默认停点

```text
[rt] assert skipped / int3 in guest code at game_scarlett_master.exe+0x344196
```

5.58 s，栈顶 `+0x344196` → `+0x51fbdb5` → `+0x164dee` → `+0x1df0038` → `+0xcc2cdd` …
`LEONIDA_SKIP_ASSERTS=1` 可以跳过它。

### 9.2 跳过后：拓扑 19 已修掉，新停点是写 GPU 页表

跳过第一个 assert 后，标题在 5.5 s 建 PSO 时被 `primitive topology type not supported: 19` 拒掉，
连带读空指针 AV。**这条已修**（见 9.3）：那是一个 Blit PSO，控制台把它当矩形列表，PC 上当三角形列表。

现在同一段路走到 11.3–11.5 s，停在一个和 PSO 无关的地方：

```text
[rt] d3d12: console primitive topology type built as a triangle list: 19
[rt] d3d12_xs: device slot 81 (forwarded) is beyond the PC device vtable (80 slots), called from 0x142482716; returning E_NOTIMPL
[rt] xgameruntime: XAccessibilityImpl slot 9 is not available offline (E_FAIL)
... AV read inside a reserved (never committed) region at game_scarlett_master.exe+0x25a087d
```

`--trace` 给出的现场（`trace-86.jsonl`）：

```text
at      game_scarlett_master.exe+0x25a087d      access=read
address 0x76af8118      region=reserve  region_base=0x76af8000  region_size=0x6ad000
gprs    rax=0x0e14b430  r9=0x689acce8  rsi=0x0da8e9b0  r14=0x0dc17580  rbx=0x1478d57e0
```

反汇编（`llvm-objdump`，`0x1425a0850`–`0x1425a088b`）：

```asm
1425a0861: movq 0x10(%rax), %rax        ; rax = page table base = 0x0e14b430
1425a0865: andl $0xf, %ecx              ; from [rsi+0xac]
1425a086b: shll $0x14, %ecx
1425a086e: orl  %edx, %ecx              ; PTE flags: bits 20.. and 24..
1425a0870: xorl %edx, %edx
1425a0872: movl (%r14,%rdx,4), %r9d     ; r9d = entry from the array
1425a0879: addl $-0x8, %r9d             ; byte offset into the table
1425a087d: movl (%rax,%r9), %r10d       ; <-- the fault: read-modify-write of a PTE
1425a0881: andl $0xfffff, %r10d
1425a0887: movl %r10d, (%rax,%r9)       ; write it back
```

写回的目标正好是 `rax + r9 = 0xe14b430 + 0x689acce8 = 0x76af8118`，也就是**在改一条 GPU 页表项**。

两个事实互斥，说明这里有一处语义对不上：

1. `r9` 是数组 `r14` 里的第 i 项（由标题自己的 getter 用 `new count*4` + 填充调用拿到）。页表项的
   字节偏移只有落在页表页里才有意义（页表页是 64 KiB 的物理页），不可能到 1.7 GB。
2. 我们的 `XMemAllocatePhysicalPages` 发出的页号是 `slot * 16`，`slot < 2^21`，也就是说页号不会超过
   `0x2000000`；而数组里的值是 `0x689accf0` 量级，比这个上限大两个数量级。

所以只有两种可能：

- **数组没被真正填过**（`new` 出来的堆内存），即那个"先取 4 字节计数、再取 `count*4` 字节"的 getter 在我们
  这边拿到了非零计数但没拿到内容；
- 或者**标题在这段代码里假定控制台地址空间可以 CPU 直接访问**：`[obj+0x10]` 是主机侧的页表基址，
  而数组里装的是控制台侧的偏移，两套地址空间混在一起算。

第二条如果成立，方向就大了：`docs/d3d12_xs_console_abi.md` §8 记着"`Map` of a unified-memory resource returns
its console address"，即标题确实会拿控制台地址当指针用；那我们的 Map/GetGPUVA 与加载器的地址空间必须一致，
并且控制台地址空间要在宿主上真的映射出来。这一条留给下一次接手判断，没有在本轮动手。

### 9.2b 两个实验把范围缩小了

**实验一：`LEONIDA_COMMIT_ON_FAULT=1`**（新增诊断：AV 落在 `MEM_RESERVE` 区域时提交那一页并重试指令，
模拟控制台"首次触碰即背书"的语义）。同一次启动里：

```text
11.381015 commit_on_fault addr=0x76cfcc48
11.381167 commit_on_fault addr=0x7725d179
11.381233 commit_on_fault addr=0x782d6d23
11.381250 commit_on_fault addr=0x7c6fd310
11.381303 commit_on_fault addr=0x7d3752d1
11.381365 commit_on_fault addr=0x7e0da463
11.381393 commit_on_fault addr=0x794a53ff
11.381502 exception 0xc0000005 addr=0x7aca9651 region=free   <- 落在一个纯空闲区，提交也不行
```

所以这**不是一个地址的问题**：那个循环每处理一个数组元素就写一条 PTE，元素一个接一个落在互不相干的地方；
而且其中至少一个落在**从未保留过**的地址上。换句话说"只是少提交了一次"解释不了它。

**实验二：给 slot 81 打出参数**（新增：超过 PC 表的设备槽位第一次调用时打印前五个参数）。boot 的调用是

```text
[rt] d3d12_xs: device slot 81 first call arguments: 0x1 0x11100 0x134f0e3cc 0x134f0e3d0 0x6475e0e88
```

反汇编调用点（`0x1424826a0` 起，`callq *0x288(%rax)` 即 vtable 槽位 81）：

```asm
1424826ef: leaq 0x2c(%rsp), %rdi        ; rdi = &kind        (输入时 kind = 6)
1424826f4: leaq 0x30(%rsp), %rbx        ; rbx = &out         (16 字节)
1424826f9: movl $0x11100, %r8d          ; r8  = 0x11100
1424826ff: movl $0x6, (%rdi)
142482705: movq %rdi, %r9               ; r9  = &kind
14248270b: movq %rbx, 0x20(%rsp)        ; 栈参数 = &out
142482710: callq *0x288(%rax)           ; slot 81
142482716: testl %eax, %eax             ; 只看 hr
142482718: movl (%rdi), %eax            ; 成功还要 kind != 0
...
14248272b: movl (%rbx), %ecx            ; out+0 = 页大小：0x10000 或 0x1000
14248273d: cmpb $0x0, 0x8(%rbx)         ; out+8 = 一个标志字节
```

调用者拿到的结构按 0x38（56 字节，`sizeof(D3D12_RESOURCE_DESC)`）步长遍历，并读 `out+0x18`。
`E_NOTIMPL` 让 `hr != 0`，调用者于是走 `0x14248276d` 的回退路径——**页表那一大块偏移就是从这里出来的**。

也就是说 slot 81 是一次"查询"（1 个描述、属性字 0x11100、输入 kind=6、输出页大小 + 标志），
它答不上来时标题退到一条没有尺寸信息的路上，随后用垃圾偏移写页表。这是下一轮最值得先实现的一条。

同一条 PSO 链上还有两处不影响停点、但迟早要补的：

```text
[rt] d3d12: multisample shader view requires a matching typed colour format: 19
[rt] d3d12_xs: slot 67 call 41 failed with 0x80070057 (kind 0 28 B, kind 1 20 B, kind 6 640 B, kind 7 344 B)
[rt] d3d12: no translated compute shader for FSPX kind-6 and kind-0 sha256 9e132485…
[rt] d3d12: no translated compute shader for FSPX kind-6 and kind-0 sha256 7b6436b0…
```

- `slot 67` 是 FSPX compute PSO 创建（`0x80070057` = `E_INVALIDARG`），失败原因就是下面那两个 shader 没有译文。
- 这两个 FSPX kind-6 程序和另外 38 个程序，已经被 `LEONIDA_DUMP_SHADERS` 抓出来了（见 9.3）。

### 9.3 这一轮修掉的：控制台拓扑 19，以及语料里缺的 Blit VS

**拓扑 19**：`runtime/shims/d3d12_vulkan_device.cpp` 的 PSO 翻译原来只认 `POINT`/`LINE`/`TRIANGLE`，
其余一律 `E_INVALIDARG`。用 `LEONIDA_DUMP_SHADERS` + 一次详情日志定位到请求者：

```text
[rt] d3d12: primitive topology type not supported: 19
[rt] d3d12:   vs VS_Blit_6e5520d1_Wrapped, ps PS_BlitColour_6e5520d1_Wrapped, 1 target(s) format0 28, dsv 20, samples 1
```

一个 Blit 管线要 5 个 MRT 之外的 1 个 RGBA8 目标、没有深度状态。控制台把矩形列表画成两个三角形
（`docs/d3d12_xs_console_abi.md` §10：`IASetPrimitiveTopology` 17 = rectangle lists），所以
**大于 `PATCH` 的取值按 `TRIANGLE_LIST` 建**，并把取值打进日志。改完这条 PSO 就建出来了。

**缺的 Blit VS**：同一时刻日志里还有

```text
[rt] d3d12: no translated vs for FSSM sha256 01ac7810dcabd62afcd904b231e90d9a031a51808a700e3349290bec5f0d426f
```

它不是 `shaders.rpf` 里的程序，语料里没有（`index.json` 里查不到这个 sha256）。这一轮的
`LEONIDA_DUMP_SHADERS` 把它和另外 39 个程序一起写了出来，其中 24 个能干净反汇编，**1 个能直接翻译**——
正是这个 Blit VS（`vs-01ac7810dcabd62a`，wave64 VS、无顶点输入、5 个 MRT、`topology type 3`）。
已按 §7.1 的链翻译、`spirv-val` 通过、并进语料（26,555 → **26,556**），重跑后 `no translated vs` 归零。

剩下 23 个多半是 compute：翻译器要 `COMPUTE_PGM_RSRC2` 和线程组大小，也就是 FSSM **头部** 0x138..0x144
的四字，而我们 dump 出来的是"程序"（运行时用来算 `fssm_sha256` 的那段代码），**不含头部**，所以报
`CS group size` / `CS thread-group-size SGPR` 而拒绝。要补它们得拿到带头的 FSSM 块（容器里那份），
不是这条 dump 路径能给的。另外 8 个反汇编失败的是 FSPX kind-6，不是 FSSM 布局。

（这条"dump 不含头部"的限制在 §9.4.1 已经解决：现在 dump 连 DXBC 容器一起写。）

---

## 9.4 03:00–04:30：dump 带容器、槽位 81 被否掉、DStorage zlib

### 9.4.1 `LEONIDA_DUMP_SHADERS` 现在也写 DXBC 容器

`dump_untranslated_shader()` 原来只写 `<stage>-<sha16>.fssm`，也就是"程序"本身——而翻译器要的东西一半在
容器里：签名（`ISG1`/`OSG1`/`OSG5`）、`PSV0`（输出寄存器、MRT），以及 FSSM 头部 0x138..0x144 的
`COMPUTE_PGM_RSRC2` 与线程组大小。所以 7.1b 那条链对 dump 出来的程序只能翻译 VS（不需要这些），
compute 会报 `CS group size`——**不是翻译器的毛病，是 dump 少了东西**。

现在 `dump_untranslated_shader(..., container, container_bytes)` 在 program 旁边再写一份 `<name>.dxbc`
（只有真的是 DXBC 容器时才写），note 里加一行 `container bytes`。`tools/rdna2spirv.py` 的
`shader_container()` 正好在 `.s` 的两级父目录找同名 `.dxbc`，所以：

```text
tmp/ps4522/fssm/ps-452212e59d983c3b.fssm   -> 反汇编到 tmp/ps4522/asm/
tmp/ps4522/ps-452212e59d983c3b.dxbc        -> 翻译器读签名/PSV0/头部
```

实测这条 PS（`452212e5…`，wave64、1 个 `R11G11B10_FLOAT` 目标 + D32 深度、无顶点输入）干净反汇编、
`spirv-val` 通过，并进语料后 **26,556 → 26,557**；重跑时 `no translated ps` 归零，它要的那个
`CreatePipelineState` 建了出来，启动继续往前走。所以那 40 个"翻不出来的程序"里，**只要 dump 里有容器**，
就能按 7.1 的链补进语料——这条路径比去 `shaders.rpf` 反查快得多。

### 9.4.2 设备槽位 81：试了两种答法，都不是页表 AV 的原因

§9.2b 的推论是"槽位 81 答不上来 → 标题走回退路径 → 用垃圾偏移写页表"。现在槽位 81 有了可切换的答法
（`LEONIDA_SLOT81=zero|page64`，默认仍是 `E_NOTIMPL`），实测：

| 答法 | 停点 |
| --- | --- |
| 不设（`E_NOTIMPL`） | 12.1–15.9 s `AV @+0x25a087d` |
| `zero`（`S_OK`，`*kind = 0`） | 13.0 s **同一个 AV，同一个索引 2073** |

并且有一次运行里槽位 81 **根本没被调用**（`trace-96`：日志里没有 `slot 81` 那行），页表 AV 照样发生。
结论：**槽位 81 与页表 AV 无关**；那条 AV 在**工作线程**上（栈底是 `KERNEL32.DLL+0x2dda7` = 线程入口），
与主线程的槽位 81 是两条线。§9.2b 的第 1 号待办可以划掉（槽位本身仍然没实现，见 §13）。

### 9.4.3 页表 AV 的真相：流读到一半就断了，而标题不看读了多少

`LEONIDA_PROBE_ARRAY=<dir>` 这个探针在 AV 那一刻把现场拆开写进 trace，做三件事：读
`[rsp+0x3c]`（这个帧的数组长度）、把 `r14` 指向的数组整段落盘、把 `[rax]` 起的页表头部 4 KiB 落盘，
再顺着 `[rsp+0x48]`（保存的第三个参数）和 `[rsp+0x50]`（保存的流对象）打出流的状态（`trace-93`）：

```text
at      game_scarlett_master.exe+0x25a087d
gprs    rax=0xe1409d0（页表基址） rdx=0x819（循环下标 2073） r9=0x689acce8（出错的数组项）
array   count=3627  min=0x10  max=0xff9b2bcf
head    0x9d8 0x9e0 0x9e8 0x9f0 0xc38 0xc40 …          <- 8 字节步长，正是 PTE 的字节偏移
tail    … 0x31c4d802 0x10 0x689accf0 0x10 …             <- 8 字节一组的堆垃圾
stream  buffer=0x147906a80  capacity=0x1000  held=0  pos=0
table   0x9d0: 00500000 606068c4 | 0x9d8: 00500001 9838d707 …   <- 确实是 PTE 表（低字 = 页号）
```

三件事对上了：

1. 页表**是真的**：偏移 0x9d0 起是 8 字节 PTE，低字 `0x00500000 + i`。所以前 2000 多项写进去是合法的。
2. 数组**前 ~8,152 字节是正常的 PTE 偏移**（正好两个 4 KiB 读缓冲），之后变成 `(随机值, 0x10)` 的配对，
   那是堆里上一任使用者留下的内容——典型的"只填了一半的缓冲区"。
3. 现场那个 `-8` 的加法（`add $0xfffffff8,%r9d`）说明数组项 = PTE 字节偏移 + 8；一个垃圾项
   `0x689accf0` 就变成 `0x689acce8` 的偏移，落到页表外，`region=reserve` 只是它恰好落进别人的保留区。

而"只填一半"的原因在**读取方**：`0x1425a0630` 这个函数分两次从流里读——先 4 字节拿个数，再
`count*4` 字节拿数组——**两处调用都把返回值丢掉了**（`call 0x1451d2260` 之后不看 `eax`）。
流不够长时它照样往下走，缓冲区剩下的就是没初始化过的堆。这条链最后由 DStorage 的 zlib 短读解释通（9.4.4）。

顺带确认：那段数组**在 exe 里搜不到**（拿 `0x9d8 0x9e0 0x9e8 0x9f0` 的小端字节串搜整个镜像，无命中），
所以数据来自运行时读的资源，不是镜像里的静态表。

### 9.4.4 DStorage 的 zlib：补上之后那条 AV 消失了

`runtime/shims/dstorage.cpp` 原来对 zlib / BCPack / swizzle 一律 `E_NOTIMPL`，日志里也就一直有

```text
[rt] dstorage: zlib, BCPack and swizzle requests are not implemented yet (E_NOTIMPL)
[rt] dstorage: an entry failed with 0x80004001 (command type 0)
```

`E_NOTIMPL` 意味着目标缓冲区**一个字节都没动**，而标题当它读成功了——于是流里的数据就是上一任内容，
和 9.4.3 观察到的"前 8 KB 正常、后面是堆垃圾"完全吻合。

做法：把 `shader_library.h` 里自带的 inflate（RFC 1950/1951 + Adler-32，纯 C++、无第三方）**拆到**
`runtime/src/zlib_inflate.h`，`shader_library.h` 只是 include 它，`dstorage.cpp` 也 include 它，
然后 `execute_zlib_request()` 变成：

```text
源（文件 read_file_range / 内存 guarded_move）-> SourceSize 字节
  -> leonida::zlib_inflate(..., DestinationSize)
  -> 长度必须正好等于 DestinationSize，否则 E_DSTORAGE_INVALID_DESTINATION_SIZE（目标一字节不动）
  -> 拷进 Destination
```

BCPack 和 swizzle 仍然 `E_NOTIMPL`（那两个是 GPU 侧的排布变换，不是解压）；`IntermediateSize` 对 zlib
必须为 0，保持原来的拒绝。

效果（`trace-97`）：

```text
14.2 s  unimplemented: acphal.dll!ApuCreateHeap / AcpHalAllocateShapeContexts
15.x s  device slot 81 routed to console page-size query
16.4 s  退出码 0xC0000409（STATUS_STACK_BUFFER_OVERRUN / fastfail）
```

**`+0x25a087d` 那条 AV 再没出现过**，启动从 12 s 推到 16.4 s。留下的新停点是音频初始化之后的 fastfail，
不是 AV、也没进异常处理器（fastfail 不走 VEH，所以 trace 里连 `exception` 事件都没有）。

### 9.5 音频的 22 个导入：把"返回 0"变成"写出参"，窗口活过 8 分钟

zlib 之后的新停点是 `0xC0000409`（fastfail）与 `0xC0000005`（写地址 `0x20`）交替出现，两者都在
`AcpHalAllocateShapeContexts` 之后。反汇编调用点（`game_scarlett_master.exe+0x43aec40` 起）把责任分清了：

```asm
1443aec68: movq $0x15e,[0x149ae98e0]              ; 350 个 shape context
1443aec73: call AcpHalAllocateShapeContexts(&0x149ae98d0)
1443aec80: r8d = [0x149ae98e0] << 6               ; r8 = 350*64 = 22400
1443aec87: rcx = [0x149ae9908]                    ; = ctx + 0x38
1443aec94: call memset(rcx, 0, r8d)               ; 写 0x20 -> AV
```

也就是 `AcpHalAllocateShapeContexts` 的职责是"把 count*64 字节的块写进 `ctx+0x38`"，而未实现导入返回 0
让这个字段一直是 0，标题就把 memset 打到空指针附近。同一段里还有三处：

```asm
1443aecf2: call ApuAlloc(&block, 0, 0x222e00, 0x800)   ; 出参在栈上，返回 HRESULT
1443aed18: r15 = block                                 ; 350 项的表用这块内存填（每项 0x1700+0x200）
1443aed91: call AcpHalCreate(&hal)                     ; 传的是 &global
1443aedb7: rax = [hal];  call *(%rax)                  ; 把 hal 当对象：取 vtable[0] 并调用
```

最后一条是关键：`AcpHalCreate` 写出的不是"不透明句柄"，而是**对象指针**——标题立刻解引用它取 vtable 并调用
第一项（参数 `(0x1000, 0x1000)`）。返回一个全零缓冲区会被 `call *0` 打死。

新增 `runtime/shims/audio.cpp`，把这条链上有实测形状的 9 个导入做成桩（`AcpHal.dll`）：

| 导入 | 桩的行为 |
| --- | --- |
| `AcpHalAllocateShapeContexts(ctx)` | 按 `ctx+0x10` 的数量提交 `count*64` 字节，写 `ctx+0x38` |
| `AcpHalCreate(out)` | 写一个对象指针，`[handle]` 是 16 项 vtable，全部返回 `S_OK` |
| `AcpHalReleaseShapeContexts(handle)` | `S_OK` |
| `ApuCreateHeap(size, flags)` | `S_OK`（首调用实测 `477102080` 字节、`flags 0x226e00`） |
| `ApuAlloc(out, align, size, flags)` | 提交页对齐的 `size` 字节，写出参 |
| `ApuFree(p)` | `VirtualFree` |
| `ApuIsVirtualAddressValid(p, size)` | 非空即 1 |
| `ApuMapVirtualAddress(p, size)` / `ApuMapApuAddress(token)` | 一张"32 位 token ↔ 指针"的小表（宿主地址 64 位，直接截断会撞车） |

`xapu.dll` 与 `chad.dll` 的 10 个仍未实现：它们的调用点还没走到非答不可的地方。

效果（`trace-101`，写这段时那次运行还活着）：**进程活过 8 分钟**，窗口 `Americas` 在前台、`Responding`，
工作集 19.9 GB，日志里连续出现 `slot 67` 的 FSPX compute PSO（`call 259` 以上）——每一条都因为
`no translated compute shader for FSPX kind-6 and kind-0` 而 `E_INVALIDARG`，标题并不因此退出。

这是本晚最好的状态：**从"15 秒必死"变成"能一直活着"**。剩下的问题是画面：全程只有一次 `PresentX ready`，
没有第二帧，窗口是白的——所以它还停在初始化/PSO 阶段，没进渲染循环。

---

## 10. 构建与测试

### 10.1 构建

```bat
tmp\build-gta6rt.cmd all        :: 全量，BUILD-OK，466 个构建步骤
tmp\configure-561b65a.cmd       :: 重新生成构建目录（构建目录 runtime\build-561b65a）
```

**ninja 不跟踪头文件依赖**：改了 `.h` 必须 `touch` 受影响的 `.cpp` 再编，否则会用到过期的 `.obj`。
这一晚被咬过一次：`LNK2019 unresolved create_vulkan_device_standalone` 就是过期 obj 造成的。

### 10.2 测试

串行 `ctest -j 1`（推荐，理由见下）：

```text
99% tests passed, 4 tests failed out of 268
```

最初是 5 项；`d3d12_vulkan_device` 在 03:0x 修好（见下表的说明），现在剩 4 项：

| 测试 | 现象 | 归因 |
| --- | --- | --- |
| `run_module_names` | 未深查 | 不清楚，本次没有碰模块命名 |
| `d3d12_depth_resolve` | 深度 resolve 路径 | 未深查 |
| `d3d12_pipeline_library_host` | 与宿主逐项对比，74 项里 2 项不一致 | 宿主自己的格式/答案差异，未深查 |
| `dxgi_swapchain` | 交换链 | 未深查 |
| ~~`d3d12_vulkan_device`~~ | 已修 | 三项检查走的是**代理的 PC 槽位**，而代理的槽位是控制台的：61 = `CreateSamplerX`（#7115）、62 = `CreateComputePipelineState`（#7163）、78/79/80 = 帧事件。于是 `CreateMetaCommand` 变成了建采样器、`CreateReservedResource2` 变成了帧事件，`meta == nullptr` 和 `tiles == 1` 必然失败。这三段改成直接问 `inner`（代理包着的 Vulkan 设备）——它们本来测的就是设备对 PC 的答案。提交 `ee38ebbaa`。 |

`-j 4` 时还会多挂 `d3d12_texture_tiles` 和 `d3d12_bindless_churn`，单独跑能过 —— 是 GPU 竞争导致的抖动，
不是代码问题。**结论：跑 ctest 用串行。**

---

## 11. 复现命令

```powershell
$env:LEONIDA_GUEST_TLS_SLOT0='1'
$env:LEONIDA_SHADERS='E:\temp\wait_work\leonida-work\shaders\spirv-561b65a'
$env:LEONIDA_D3D12='vulkan'
$env:VK_LOADER_LAYERS_DISABLE='~implicit~'
# 需要往前多探一段时：
$env:LEONIDA_SKIP_ASSERTS='1'

.\runtime\build-561b65a\gta6rt.exe `
  --run "E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK\game_scarlett_master.exe" `
  --game-dir "E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK" `
  --trace tmp\trace-78.jsonl
```

**直接双击 `game_scarlett_master.exe` 一定失败**，Windows 会报找不到 `d3d12_xs.dll`、`xmem.dll`、`xg_xs.dll`、
`chad.dll`：这四个是控制台系统的 DLL，由 `gta6rt` 在**进程内**映射并提供，磁盘上根本没有对应的文件。所以只能经
`gta6rt --run` 启动。仓库里有一个能双击的封装：`tmp\play-gta6.cmd`（不带参数就启动并把日志写到 `tmp\play.log`，
带 `trace` 参数额外写 `tmp\play-trace.jsonl`，结束时打印最后 25 行并 `pause`）。

可用的排查开关：

| 开关 | 用途 |
| --- | --- |
| `--trace <file>` | 记录 map / xmem / 异常与时间戳（本文所有秒数都出自它） |
| `LEONIDA_SKIP_ASSERTS=N` | 跳过前 N 个 guest `int3`（#7204） |
| `LEONIDA_SKIP_INT3=1` | 同上的旧开关 |
| `LEONIDA_UNIMPLEMENTED=log\|stop\|fail` | 未实现导入的策略 |
| `LEONIDA_DUMP_SHADERS=<dir>` | 语料里没有的 shader 写到该目录（见 7.1b、9.4.1；现在连 `.dxbc` 容器一起写） |
| `LEONIDA_PROBE_ARRAY=<dir>` | 页表写入 AV 那一刻把数组、页表头、流状态写进 trace 并落盘（见 9.4.3） |
| `LEONIDA_SLOT81=zero\|page64` | 设备槽位 81 的两种实验答法（见 9.4.2；默认仍是 `E_NOTIMPL`） |
| `LEONIDA_TRACE_PSO=1` | 每解析一条 pipeline state stream 就打一行 subobject 走位 |
| `LEONIDA_COMMIT_ON_FAULT=1` | 诊断用：AV 落在 `MEM_RESERVE` 时提交那一页并重试（见 9.2b） |
| `--trace-d3d12` | **别在长时间运行里用**，日志量会把进程拖到看门狗的 30 秒门槛 |

退出码 `0x80000003`（`-2147483645`）= 撞上 guest `int3`，`0xc0000005` = 真 AV。两者要分开看。

---

## 12. 这一晚踩过的坑（省下一次重复劳动）

1. **合并上游 PR 会冲突在 `runtime/shims/d3d12_xs_device.cpp`**：同一个函数的整段互相覆盖。别手改，
   用 `git show <rev>:<path>` 取上游原文再拼；需要时写 `tmp/*.py` 脚本做文本替换。
2. **不要用 `python -c`** 改文件（引号/编码在 PowerShell 下会翻车，`.remove-file EIO`、乱码都遇到过），
   一律写脚本文件再执行。
3. **`reinterpret_cast` 撞上重载函数**（`configure_virtual_memory` 有两个版本）→ C2440。只留实测的那个 4 参数版本。
4. **`GetCustomHeapProperties` / `GetResourceAllocationInfo` 是隐藏返回**：MSVC 里按返回值取，别按 out 参数写。
5. **槽位 133 需要完整 192 槽表**：表只有 128 槽时是空指针，先扩表再谈处理。
6. **`RegisterClassExA` 返回 0 标题当致命**：转发到宿主 user32 但不把失败吞掉，日志里要能看见。
7. **测试和实现一起改**：恢复代理命中计数这类改动会让老测试失败，必须同步把测试改成新行为，而不是把计数改回去。
8. **后台分片任务要留 `inflight-N.txt` 记号**：`shard-7/9` 就是靠它和 `.err` 里的 "still compiling … after 8703s"
   才确认是卡死而非崩溃。

---

## 13. 下一次接手的第一梯队

按 04:30 的状态排序。**先读这一条**：现在的停点不再是"崩"，而是"活着但不渲染"——窗口 `Americas`
在前台、`Responding`、进程能一直活着，只有一次 `PresentX ready`、没有第二帧、画面全白。

1. **FSPX compute 的语料**（当前最硬的一条）：`slot 67` 已经在几百次调用上失败，每条都是
   `no translated compute shader for FSPX kind-6 and kind-0`。实测 270 次调用里 **270 个不同 sha256**，
   但只有 **176 个不同的 (kind 6, kind 7) 尺寸对**——说明 `sha256` 键把 kind 0（dispatch state）也算进去了，
   而 kind 0 每次都在变。所以先别急着翻译 270 个程序：先用 §9.4.1 的新 dump（连容器一起写）把它们抓下来，
   看清"真正不同的 kind 6 程序有几个"。若只有几十个（`amd_fidelityfx_upscaler_xbox.dll` 里就 29 个），
   就按 `docs/fspx.md` 的链翻译；若真是几百个，那是**查找键**的问题，不是语料的问题。
   语料里 `fspx/<name>.json`（kind 0 的 word 3/4/6：线程组、wave32、`COMPUTE_PGM_RSRC2`）就是翻译器要的头部。
2. **画面为什么还是白的**：只有一次 `PresentX ready`，没有第二帧。要么标题卡在初始化（它每 20 秒才建一个
   FSPX PSO，说明中间在做重活），要么第一帧提交了但交换链没把图像拷出来。先看
   `LEONIDA_TRACE_PSO`/`--trace` 在 8 分钟里有没有 `Present` 事件；没有就是没进渲染循环。
3. **`xapu.dll` / `chad.dll` 的 10 个导入**：仍是未实现。`XApuConnect(&a,&b,&c)` 有三个出参
   （`+0x43aee69` 实测），`XApuEnqueueCommand`/`XApuDequeueResult` 共享一个全局句柄；音频命令队列一旦被真正用到，
   这三个出参就得像 §9.5 那样写出真东西。
4. **其余 compute 程序的语料**（若 §1 判定是真缺程序）：按 §9.4.1 的链（dump 带 `.dxbc` → 反汇编 → 翻译 → 并库）。
5. **`d3d12_depth_resolve` 的"拒绝却改写了目标"**：`Close` 已经是 `E_INVALIDARG`、批处理也被拒
   （`list 0 is invalid; refusing batch`），但目标纹理的字节和清屏值不一致，而
   `resolve_and_read(..., run_invalid=true)` 期望目标一字不改。得查是"拒绝前已经在某处落盘"
   还是测试自己复用了缓冲区。
6. **设备槽位 81 本身**（9.4.2 已证明它与页表 AV 无关，但仍是未实现的槽位）：形状是
   `(token, 0x11100, &count=6, out[6] 每项 0x38 字节)`，调用方读 `out+0`（页大小 0x10000/0x1000）、
   `out+8`（标志字节）、`out+0x18`（句柄）。`LEONIDA_SLOT81=zero|page64` 是现成的实验开关。
7. **代理的 PC 槽位与控制台槽位冲突**（`ee38ebbaa` 修了测试这一侧）：61/62/78/79/80 在两边含义不同。
   以后任何"PC 形状的调用者"都不能直接拿代理当 `ID3D12Device*` 用；要么问 `inner`，要么让代理按调用者分流。
8. **那个"流读到一半"的读取方**（9.4.3）：`0x1425a0630` 丢掉两次读的返回值，是我们这边数据短了才暴露的。
   zlib 补上后它没再犯，但如果以后又看到"前 N KB 正常、后面是堆垃圾"的数组，先怀疑**上游少给了字节**，
   而不是数组本身。
9. BCPack / swizzle 的 DStorage 请求仍是 `E_NOTIMPL`（GPU 侧排布变换，需要先有 texture layout 那一套）。
10. `pipeline_cache` 里 `shard-7/9` 那两个 `indirect_diffuse_lighting_CS_ScreenProbes_*` 模块：需要先弄清是
    翻译器不收敛还是驱动侧的问题，再决定要不要单独跑。

---

## 14. 仓库状态

- 分支 `main`，**领先 `origin/main` 100 个提交**（`origin/main` = `dfe4e4ad3`），0 落后，工作树干净
  （只剩未跟踪的 `.idea/`、`tmp/`）。注意：`origin/main` 在 02:5x 被**本会话之外的进程** fast-forward 过
  （reflog 里是 `fetch origin --recurse-submodules=no --progress --prune`，不是本会话发的），上游已到
  `dfe4e4ad3`（`d3d12: guard null pInputElementDescs and validate graphics/compute PSO descriptors (#6689)`）；
  按用户要求**没有合并**。
- 本晚的提交（全部本地）：
  `13d2785b0`（本记录）、`d435276bb`（拓扑 19 + 四组诊断）、`946ef794c`（slot 81 参数 +
  `LEONIDA_COMMIT_ON_FAULT`）、`ed61a963b`（9.2b 更新）、`ee38ebbaa`（设备测试改问 `inner`）、
  `ad5e665cf` + `3f2481c8c`（文档）、`69183c269`（dump 带 `.dxbc`）、`091db69f2`（页表数组探针）、
  `985a3e30e`（槽位 81 的两种答法）、`d770c68e9`（DStorage zlib）、`e45f1dbe3`（音频 9 个导入的桩）。
- 语料在仓库外：`E:\temp\wait_work\leonida-work\shaders\spirv-561b65a`，本轮 26,556 → **26,557**（那条 PS）。
- **从未推送**，也不打算推送（用户明确：不做 PR，改为同步上游）。
- 本文档与代码提交分开：改文档时只提交这一个文件。
