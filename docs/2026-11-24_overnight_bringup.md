# 2026-11-23 20:00 → 2026-11-24 02:18 夜间攻坚记录

零售版 `game_scarlett_master.exe`（GTA VI Xbox，1.0.2.1）在本机 Windows 上经 D3D12→Vulkan 层启动的
一次性记录。范围是这一晚实际做的事、测到的数、以及下一次接手时应该从哪里继续。

不是上游 PR 的说明文档，只是本次会话的工程日志；结论都能在本文引用的日志、trace 或提交里复核。

---

## 1. 一句话总结

一晚把启动链从"进入 guest 后很快停住"推进到两处可复现的硬停点：

- **默认（不跳过 assert）**：`int3` 在 `game_scarlett_master.exe+0x344196`，进程启动后 **5.58 s**。
- **跳过第一个 assert（`LEONIDA_SKIP_ASSERTS=1`）**：跑到 **11.27 s**，在 console PSO 的
  `primitive topology type not supported: 19` 之后读空指针 AV（`+0x25a087d`）。

并且确认了此前 300 秒级"卡死"的真实原因：fence 槽位 12 返回 `E_NOTIMPL`，标题进自旋，最后由它自己的
30 秒看门狗写给地址 0（`+0x2215c35`）。合并上游 PR #7290 后该自旋消失，单次启动从 309 s 掉到 11.27 s。

---

## 2. 起点与终点

| 项目 | 起点（11-23 20:00 前后） | 终点（11-24 02:18） |
| --- | --- | --- |
| 上游基线 | `561b65a` | `a779d6411`（384 提交，合并提交 `b7b7e8151`） |
| 本分支 | 刚从 `561b65a` 拉起 | `5bf5ad4f0`，领先 `origin/main` **102** 提交，落后 0，全是本地提交，未推送 |
| 构建 | 部分目标 | 全量 `all` = BUILD-OK（466 个构建步骤） |
| 测试 | 未跑 | 串行 ctest：**98% 通过，268 项里 5 项失败**，237.95 s |
| 启动 | 内存表面/描述符堆一带停住 | 5.58 s（默认）/ 11.27 s（跳过 1 个 assert），停在 console PSO 拓扑 19 |
| 管线缓存 | 无可用缓存（启动时现场编译） | `pipeline_cache.bin` 760,673,388 B，放在 `gta6rt.exe` 旁边 |
| 着色器语料 | 26,503 条 | 26,555 条（新增 52 个 `XboxBvhBuilder.dll` 内的 compute shader） |

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

### 9.2 跳过后（11.27 s）

```text
[rt] d3d12: primitive topology type not supported: 19
[rt] d3d12_xs: device slot 81 (forwarded) is beyond the PC device vtable (80 slots), called from 0x142482716; returning E_NOTIMPL
[rt] xgameruntime: XAccessibilityImpl slot 9 is not available offline (E_FAIL)
... AV read 0x76cf73a8 at game_scarlett_master.exe+0x25a087d
```

这条链是自洽的：console PSO 用了一个 PC 没有的原语拓扑（19），我们的 PSO 翻译直接拒了，PSO 没建成，
标题后面拿这个 PSO 去解引用就 AV。所以**下一个真正要修的是 console PSO 的原语拓扑 19**（以及紧随其后的
设备槽位 81 超出 80 槽的 PC vtable）。

同一时刻日志里还有两处相关线索：

```text
[rt] d3d12: multisample shader view requires a matching typed colour format: 19
[rt] d3d12_xs: slot 67 call 41 failed with 0x80070057 (kind 0 28 B, kind 1 20 B, kind 6 640 B, kind 7 344 B)
[rt] d3d12: no translated compute shader for FSPX kind-6 and kind-0 sha256 9e132485…
[rt] d3d12: no translated compute shader for FSPX kind-6 and kind-0 sha256 7b6436b0…
```

- `slot 67` 是 PSO 创建（`0x80070057` = `E_INVALIDARG`），kind 6/7 的字节数说明描述符流里混着控制台专有块。
- FSPX 那两个 shader 在 `amd_fidelityfx_loader_xbox.dll` 里，用第 7.1 节同一条 carve→translate 链就能补进语料。

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
98% tests passed, 5 tests failed out of 268     Total Test time (real) = 237.95 sec
```

失败项：

| 测试 | 现象 | 归因 |
| --- | --- | --- |
| `run_module_names` | 未深查 | 不清楚，本次没有碰模块命名 |
| `d3d12_vulkan_device` | `R10G10B10` 的拷贝/resolve 被 `invalid copy or resolve` 拒 | **怀疑**与今晚合进来的 A8/格式规则有关，尚未证实 |
| `d3d12_depth_resolve` | 深度 resolve 路径 | 未深查 |
| `d3d12_pipeline_library_host` | 管线库宿主路径 | 未深查 |
| `dxgi_swapchain` | 交换链 | 未深查 |

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

可用的排查开关：

| 开关 | 用途 |
| --- | --- |
| `--trace <file>` | 记录 map / xmem / 异常与时间戳（本文所有秒数都出自它） |
| `LEONIDA_SKIP_ASSERTS=N` | 跳过前 N 个 guest `int3`（#7204） |
| `LEONIDA_SKIP_INT3=1` | 同上的旧开关 |
| `LEONIDA_UNIMPLEMENTED=log\|stop\|fail` | 未实现导入的策略 |
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

1. **console PSO 原语拓扑 19**（`runtime/shims/d3d12_vulkan_device.cpp` 的 PSO 翻译）→ 顺带看 `slot 67` 的
   `E_INVALIDARG`（kind 6/7 的控制台块）。
2. **设备槽位 81 超出 80 槽 PC vtable**：要么扩路由表，要么找到它在 PC 侧的等价槽位。
3. **FSPX 两个缺翻译的 compute shader**（`9e132485…`、`7b6436b0…`）：第 7.1 节的 carve→translate 链补语料。
4. **`d3d12_vulkan_device` 的 `R10G10B10` 拷贝失败**：唯一一条代码级红项，和 A8/格式规则合并有关。
5. 音频 `AcpHal` / `xapu` / `chad` 的未实现导入（上游 A20 的 open ask）。
6. `pipeline_cache` 里 `shard-7/9` 那两个 `indirect_diffuse_lighting_CS_ScreenProbes_*` 模块：需要先弄清是
   翻译器不收敛还是驱动侧的问题，再决定要不要单独跑。

---

## 14. 仓库状态

- 分支 `main`，HEAD `5bf5ad4f0`，**102 个提交领先 `origin/main`，0 落后**，工作树干净（只剩未跟踪的 `.idea/`、`tmp/`）。
- **从未推送**，也不打算推送（用户明确：不做 PR，改为同步上游）。
- 本文档本身是一次独立提交，只含这一个文件。
