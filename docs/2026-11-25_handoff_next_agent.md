# 交接文档：Leonida（GTA VI Xbox 1.0.2.1 → PC）下一任 agent 从这里开始

> 写于 2026-11-25 08:40 · 交接人：上一轮 agent · 仓库 `E:\temp\wait_work\Leonida`（**本地提交，从未 push**）
> 详细技术档案在 [`2026-11-25_title_spins_on_pipeline_state.md`](2026-11-25_title_spins_on_pipeline_state.md)
> （§0–§21，含全部实测数据）。本文是独立可读的摘要 + 操作手册，两者冲突时以那份为主。

---

## 0. 一句话现状

**游戏能启动、能稳定跑、但呈现的帧是纯黑，不能玩。**
启动完成后它会建 49,152 个图形管线、读 3.76 GB 数据，然后进入稳态：每秒 717 个 draw
（4 个/帧）、~195 Hz 呈现、2.4 核 CPU、**85 个 D3D12 入口点不再增加**、从不 `Dispatch`、
从不 `DrawIndexedInstanced`。

**唯一有明确验证路径的线索**在 §5：**顶点缓冲里的颜色是白的，呈现出来却是全零。**

---

## 1. 唯一最重要的事：一个硬矛盾

| 事实 | 怎么测出来的 |
| --- | --- |
| 每帧那 4 个 draw 是 `VS_Blit` + `PS_BlitColour`，两个 stage 都是 **0 个 set-0 绑定** | `LEONIDA_LOG_REPLAY_DRAW=1 LOG_DRAW_PIPELINE=1` |
| blit 的**顶点缓冲里颜色是白的**（`0xffffffff`），几何是全屏三角形 | `LEONIDA_LOG_VB=1` → `view 0 at 0x4de0e4ef30 -> 0x2fc0ef30, 4080 bytes, stride 24; first words bf800000 3f800000 00000000 ffffffff …`（顶点 = {pos.xyz, **白**, uv.xy}，三个顶点 `(-1,1) (-1,-1) (1,-1)`） |
| `PS_BlitColour` 只读顶点属性，不采样任何纹理 | 语料 `PS_BlitColour_6e5520d1_Wrapped.spvasm`：只有 `%in_attr0 Location 0` + `OpLoad %v4float %out_mrt0` |
| `VS_Blit` 有 **3 个顶点输入**（Location 0/1/2），颜色来自属性 | 语料 `VS_Blit_6e5520d1_Wrapped.spvasm`：`vin0/1/2 Location 0/1/2` |
| **但呈现出来的帧是全零** | `LEONIDA_CAPTURE=<dir> CAPTURE_EVERY=200 CAPTURE_LIMIT=2`，生成的 `index.txt` 里 `mean rgb 0.00 0.00 0.00`（两帧都是） |

⇒ **标题在画一个白色的全屏 blit，我们却呈现出纯黑。** 矛盾只可能在三个地方：

1. **顶点属性没喂进去**（input layout / attribute offset 错 ⇒ 插值出 0 ⇒ 黑）。
   已知：**stride 24，颜色在第 12 字节**（`{pos.xyz, colour.rgba8, uv.xy}`）。
   对照语料 index 里的 `input_signature` 与 `runtime/shims/d3d12_vulkan_device.cpp`
   里 `bindings.push_back({element.InputSlot, 0, …})` / `attributes` 的 offset。
2. **blit 画的目标不是被呈现的那张资源**。比对 `LOG_REPLAY_DRAW` 的 `colour 0 id …`
   和 present 日志的 `first present plane: … resource …` / `format 28`。
3. **呈现路径自己合成了一张黑图**。看 `runtime/src/vulkan_present.h`（compose / plane 逻辑）。

**按这个顺序查，每一步都有现成开关，不需要新写代码。**

---

## 2. 环境与路径

| 项 | 值 |
| --- | --- |
| 仓库 | `E:\temp\wait_work\Leonida`（CMake + MSVC；`runtime/` 下 393 个 `.cpp/.h`，另加 `tools/` 的 Python 工具） |
| 游戏包 | `E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK`，标题 `game_scarlett_master.exe` |
| 工作目录 | `E:\temp\wait_work\leonida-work` |
| **SPIR-V 语料** | `E:\temp\wait_work\leonida-work\shaders\spirv-561b65a`（**27,372 个 `.spv`**，每个配一份 `.spvasm`，加 56 MB 的 `index.json`） |
| 构建目录 | `runtime\build-561b65a`，产物 `gta6rt.exe` |
| Python 环境 | `E:\temp\wait_work\leonida-venv\Scripts\python.exe`（**不要新建环境**） |
| ctest | `E:\temp\wait_work\tools\cmake-4.5.1-windows-x86_64\bin\ctest.exe` |
| Vulkan SDK | `E:\VulkanSDK\1.4.363.1`（`spirv-as`/`spirv-val` 在这里） |
| 本机代理 | `127.0.0.1:7897`（HKCU `Internet Settings` 的 `ProxyServer`） |
| 兄弟库（参考实现） | `E:\temp\wait_work\` 下的 `victory-gta6`、`AnyXboxX`、`xcl`、`five-star-recomp` |
| REA 接入约定 | `E:\xhqss\agent_work\dsh\doc\2026-11-15_rea-on-demand.md`（**走 CLI，不要注册 MCP**） |

---

## 3. 怎么构建、怎么跑、怎么验

### 3.1 构建（**注意：这条命令会杀掉正在跑的游戏**）

```powershell
cd E:\temp\wait_work\Leonida
Get-Process gta6rt -ErrorAction SilentlyContinue | Stop-Process -Force
Get-ChildItem runtime\shims,runtime\src -Filter *.cpp | ForEach-Object { $_.LastWriteTime = Get-Date }  # 强制重编
cmd /c tmp\build-gta6rt.cmd gta6rt   # 看到 BUILD-OK 才算成功
```

### 3.2 跑一次（跑固定秒数后杀掉，日志落 `tmp\<tag>-<hhmmss>.log`）

```powershell
Get-Process gta6rt -ErrorAction SilentlyContinue | Stop-Process -Force
powershell -NoProfile -File tmp\run-probe.ps1 -Seconds 30 -Tag myrun -Set 'XS_EMPTY_CS=1'
```

`-Set` 里写 `NAME=value`，脚本会自动加 `LEONIDA_` 前缀。脚本会预设
`LEONIDA_GUEST_TLS_SLOT0=1`、`LEONIDA_SHADERS`、`LEONIDA_D3D12=vulkan`、`LEONIDA_SKIP_ASSERTS=1`。

### 3.3 回归基线（改完必须核对）

| 指标 | 正常值 |
| --- | --- |
| 默认配置 30 s 的 presents | **3,000–6,500**（波动大，取决于落在启动哪一段） |
| assert（`assert at rdi`） | **0** |
| `device fault` | **0** |
| ctest | **固定 6 个既有失败**，一个都不该多 |

```powershell
cd E:\temp\wait_work\Leonida\runtime\build-561b65a
E:\temp\wait_work\tools\cmake-4.5.1-windows-x86_64\bin\ctest.exe -j8
# 既有失败：run_module_names / d3d12_com(SEGFAULT) / d3d12_vulkan_device /
#           d3d12_depth_resolve / d3d12_pipeline_library_host / dxgi_swapchain
```

---

## 4. 这一轮做完了什么（按价值）

| # | 成果 | 证据 |
| --- | --- | --- |
| 1 | **进程内线程采样器**（新文件 `runtime/src/thread_sampler.cpp`，`LEONIDA_THREAD_MAP=<秒>`）：挂起→一次 `GetThreadContext`→`RtlVirtualUnwind` 真走栈→`GetThreadTimes` 算 CPU%；`gta6rt.exe` 的帧用 dbghelp + 322 MB PDB 解成函数名 | 抓到"13 个线程 100% 在 amdvlk64.dll 里"，栈底是 `PipelineState::create_compute` ← `proxy_create_compute_pipeline_from_native_code` ← 标题 `+0x24800db` |
| 2 | **证明 CPU 烧在驱动编译管线**，不是标题自旋 | 标题自己的 98 个线程全部 parked；CPU 全在 amdvlk |
| 3 | **compute 管线：串行化 + 按 sha256 缓存** | CPU 1609%→**231%**，presents 4800/40 s → **10560/40 s** |
| 4 | **图形管线：按 pipeline-state-stream sha256 缓存**（**不**跨调用持锁） | 默认 6360/30 s →（错误地持锁）**300** →（只缓存）**4620/30 s** |
| 5 | **量化了图形管线风暴**：~49,152 次调用 / **48,205 个不同**（hits 1.9%） | `LEONIDA_LOG_PSO_COUNT=1` 的 census |
| 6 | **纠正两条旧结论**：①标题**读过盘**（3.76 GB，之后 25 s 只有 6 次/310 KB）；②标题**不是**"资产加载前卡住"，它跑完了启动并进入稳态 | 进程 I/O 计数器 / 稳态调用画像 |
| 7 | **纹理 `Map` 给 host 内存**（`LEONIDA_XS_TEXTURE_MAP`，默认开） | 之前 2 次 `E_INVALIDARG`；之后成功，但标题 map 两张纹理后**一字节都没写** |
| 8 | **把"每帧 4 个 draw 是什么"查清**：`VS_Blit` + `PS_BlitColour`，0 绑定 | `LOG_REPLAY_DRAW` |
| 9 | **发现白 blit vs 全零帧的矛盾** | 见 §1 |
| 10 | **上游对比**：他们还停在 stop 13 | 见 §8 |

**成果 3/4 是默认生效的真实性能修复（7 倍 CPU、2 倍帧率）；其余是把嫌疑一个个划掉。**

---

## 5. 已经证实的事实（不要重新推导）

- 描述符堆：标题用**它自己分配钩子返回的地址**当 console 的"统一地址"基（不是 `Get*HandleForHeapStart`），
  所以只改 getter 不能把它的写入搬进 arena。
- **每帧绑的那张 per-draw 表，在稳态（启动结束之后）也是空的**：arena 侧 2048/2048 全零，
  标题自己那份 2038 零 + 4~7 个非零槽（那些是 float 和 drawable 头 `0x8e3`/`0xee4`，不是描述符）。
  标题每秒建 239 个 SRV、478 个 CBV，**没有一个落进这张表**。
- 纹理 `Map` 现在能拿到内存，但标题 **map 4 MiB 和 1 MiB 两张纹理、0 字节写入**、随即 unmap。
- `device slot 81`（资源终结器，241/s）是稳态里**唯一被拒绝**的调用；`LEONIDA_SLOT81=zero|page64`
  两种回答都试过，**行为不变**。
- `xg_xs` 的两个 E_NOTIMPL **不是硬失败**（`XGSuggestSwizzleMode` 的返回值没有调用点分支；
  shim 故意在失败时仍写 `*out`）。
- 音频三连（`XApuConnect`/`XDspConnect…`/`XApuGetResultCount`）**各只被调用一次**，不是轮询；
  音频其余部分（`ApuCreateHeap`/`AcpHal*`/HAL 对象）都通。
- 帧节拍不是原因：`XS_PACE=1` 把 present 从 3720/35 s 压到 420/30 s，**CPU 一点没降**。
- **给足时间不是原因**：15 分钟不打断的 run，管线计数在 2–3 分钟停在 49,152，之后 8 分钟不动，
  入口点仍是 85。
- 稳态的调用画像（每秒）：`480 WaitFrameEventX / 478 CreateCBV / 241 slot81 / 239 SRV /
  1195 Barrier / 957 bottom-of-pipe 写 / 717 DrawInstanced（+717×3 个 per-draw setup 槽）`。

---

## 6. 已关闭的线索（**别再花时间**）

1. **描述符链路**（写过三级开关 `XS_DESC_SYNC_CPU` / `XS_TABLE_MIRROR` / `XS_MIRROR_VBASE`：
   它确实让 arena 里有内容了，但**不是那张表里的描述符**）。稳态复查后封闭。
2. **纹理 `Map`**（已实现，标题不写）。
3. **`device slot 81`**（两种回答都不变）。
4. **`xg_xs` 的 E_NOTIMPL**、**音频三连**（理由见 §5）。
5. **呈现频率 / 帧节拍**、**"再等久一点"**。
6. **上游**（见 §8）。

---

## 7. 坑点（这一轮实际踩到的，按代价排序）

1. **`tmp\build-gta6rt.cmd` 开头会 `Stop-Process gta6rt`** —— 测量跑动的时候 build 会**把它杀掉**，
   `run-probe.ps1` 会报 "exited on its own after 41s"，而日志里没有任何退出原因。
   判据：日志在 present 中途截断、没有 assert 也没有 exit 行 ⇒ **先怀疑自己**。本轮犯了三次。
2. **`git fetch` 必须走代理**（直连三次都是 `Connection was reset`）：
   ```powershell
   git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 fetch --no-tags origin
   ```
   只对这条命令生效，**不要**改全局 `git config`，也不要动 `clash-verge.yaml` 之类。
3. **不要用 PowerShell 写项目文件**（`Set-Content`/`Out-File` 可能写 BOM 或改换行）。
   用文件工具 `edit`/`write`。本轮犯过一次，事后核对（无 BOM、LF 完整）才敢留下。
4. **`Select-String ... | Select-Object -Last N` 会把关键行截掉**，产生"问题消失了"的假结论。
   **先数数量**（`(Select-String ... | Measure-Object).Count`），再看内容。
5. **stderr 有缓冲**：被 `Stop-Process` 杀掉时，末尾几行可能丢失。判断"某条日志没出现"之前先确认进程是干净退出还是被杀。
6. **ctest 不能和游戏同时跑**：并发会给出两个假失败（`d3d12_texture_tiles`、`d3d12_bindless_churn_validated`）。
   先 `Stop-Process gta6rt`。
7. **语料是生成物**：任何实验改动 `.spv` 之后要**还原并核对 sha256**。做法：改之前先
   `Get-FileHash <file> -Algorithm SHA256` 记下来，改完再核对一遍；本项目的基线哈希记在
   `2026-11-25_title_spins_on_pipeline_state.md` 里。本轮**没有改动任何语料**。
8. **单次 presents 数波动很大**（3,000–6,500 / 30 s，取决于落在启动哪一段）。判断回归要看
   **assert/fault=0 + ctest 不新增**，别只看帧数。
9. `run-probe.ps1` 默认带 `LEONIDA_SKIP_ASSERTS=1`；但那个断言在 §12 已经**真修掉**了，
   想验真伪要自己去掉这个变量跑。
10. 长跑（>60 s）要用后台任务（`run_in_background`），**期间不要 build、不要 Stop-Process**。

---

## 8. 上游（Leonida origin/main）现状 —— 不是修复来源

- 远端最新 tip：`51a6074d2`（用代理 fetch 到）。本地 `main` 相对它 **ahead 148 / behind 1521**。
- **他们的 `docs/retail_boot_chain.md` 在最新 tip 上仍然写着 "Current stop: 13"** ——
  `CreateDescriptorHeap` 之后标题断言 CPU handle 等于它自己held 的值。**我们几天前就过了这一步**
  （返回标题自己那份 XMem 地址即可）。
- 他们的 6 条 open lead（`docs/boot_leads.md`：TLS index 0 / feature level / replay 命令列表 /
  描述符字是 32 位 GPU 地址 / 语料数差 195 / 帧节拍统计）**没有一条是我们的瓶颈**；
  全部 docs 里搜 `black frame|black screen|nothing renders|draws nothing|empty frame` **零命中**。
- 他们**还没有**我们已有的两件事：纹理 `Map` 仍返回 `E_INVALIDARG`；
  `pipeline_cache.bin` 仍只读（`GetPipelineCacheData`/`save_pipeline_cache` 出现 0 次）。
- 唯一值得顺手抄的：`4ff9a77f1`（`runtime/shims/d3d12_com.cpp` 的 scissor 上限对齐），与黑屏无关。
- 有用的是**兄弟库**：`five-star-recomp` 的 `docs/d3d12x-shim.md`、`docs/gpu-resources.md`
  （描述符 window 模型、`Map` 的语义、CPU-only 堆）、`victory-gta6` 的 `memory.rs`（页目录）、
  `xcl` 的 console 堆语义。

---

## 9. 可用的诊断开关（全部 opt-in，默认关，除注明外）

| 开关 | 作用 |
| --- | --- |
| `LEONIDA_THREAD_MAP=<秒>` | 进程内线程采样：谁在烧 CPU、parked 线程的调用链、`gta6rt.exe` 帧带函数名 |
| `LEONIDA_LOG_PSO_COUNT=1` | 槽 86/67 管线创建普查：`calls / distinct / hits`，每 4096 次一行 |
| `LEONIDA_LOG_SLOT67=1` | 每次 slot 67 的程序 sha256 + root signature + 各段大小 |
| `LEONIDA_SLOT_PROFILE=1` | 每秒打印最热的 6 个 device / 6 个 list 槽 |
| `LEONIDA_LOG_VB=1` | 顶点缓冲：原地址、翻译后地址、stride、**前 16 个 dword 的真实内容** |
| `LEONIDA_CAPTURE=<dir>`（+`_EVERY`、`_LIMIT`） | 把呈现的帧落成 PPM；`index.txt` 里带 **`mean rgb`**（判黑白最快） |
| `LEONIDA_LOG_TEXTURE_MAP=1` | 纹理 `Map`/`Unmap` 次数 + 影子里的非零字节数 |
| `LEONIDA_LOG_REPLAY_DRAW=1` + `LOG_DRAW_PIPELINE=1` | 每次 draw 的 vs/ps 名字+哈希、目标、viewport、pipeline |
| `LEONIDA_LOG_ROOT=1` / `LOG_TABLE3` / `LOG_TABLE4=<stride>` | 根参数 / 根表三方差值 / 表普查 |
| `LEONIDA_XS_EMPTY_CS=1` | 语料缺 compute 程序时用内置 no-op 模块建管线。**不开时 slot 67 每帧失败**（`0x80070057`），开了才走得远 |
| `LEONIDA_XS_PACE=1` | 按 `SetFrameIntervalX` 的间隔睡到下一帧起点 |
| `LEONIDA_DUMP_SHADERS=<dir>` | 导出语料缺失的 FSPX payload 与程序 |
| `LEONIDA_PROFILE_LAYER=1` | 层内 submit/present/allocate/pipeline 的耗时统计（每 2 s） |

---

## 10. 逆向（REA）怎么用

按 `E:\xhqss\agent_work\dsh\doc\2026-11-15_rea-on-demand.md`：**用 `rea` CLI，不要注册 MCP**
（170 个工具、tools/list 1.95 MB，装不进上下文）。

```powershell
$env:GHIDRA_INSTALL_DIR='E:\software\Ghidra'
rea decompile 'E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK\game_scarlett_master.exe' 0x1424800db `
    --provider ghidra --snapshot tmp\rea\gta6.snap.json --format md | Out-File tmp\rea\dec.md -Encoding utf8
```

- **`--token-limit` 不能和 `--format json` 一起用**（报 `UNSUPPORTED_OUTPUT_COMBINATION`）：
  要么去掉，要么把输出重定向到文件再用文件工具抽样读。
- Ghidra 必须**跑完整个 185 MB PE 的 auto-analysis** 才提供服务
  （"incomplete analysis does not expose partial results"），本轮它没跑完（已停掉、释放 2 GB）。
  **第一次要做的事：把它跑完，存好 snapshot**，之后所有命令都能复用。
- 拿到 snapshot 后值得取的 RVA（都是 parked 线程栈上的调用点）：
  `0x24800db`（slot 67 调用点）、`0x247d413`、`0x249987c`、`0x53ec05e`（那条 8 帧链），
  以及 `0x2481f73`（稳态 56% 的那个线程）。
- 想快速找"标题在等什么"，另一个更便宜的办法：**看标题自己写的文件**
  —— `GTA6.Xbox.v1.0.2.1-CYBERLEEK\overlay\.gta6rt-temp\telemetrycache.dat`（2 MB，运行时会更新）。

---

## 11. 代码地图（改哪里）

| 位置 | 是什么 |
| --- | --- |
| `runtime/src/main.cpp` | `gta6rt` 入口；`--run` 起游戏线程；挂 `thread_sampler`、`guest_asserts` |
| `runtime/src/thread_sampler.cpp/.h` | 本轮新增的进程内线程采样器 |
| `runtime/src/pe_loader.cpp` | 手工映射 PE、绑导入、注册 `.pdata`（**栈能 unwind 就是因为它**） |
| `runtime/shims/d3d12_xs.cpp` / `d3d12_xs_device.cpp` | **Xbox D3D12.X → PC D3D12 的 shim**（槽号映射、描述符堆、slot 67/70/81、bottom-of-pipe 写、控制台 submit） |
| `runtime/shims/d3d12_vulkan_device.cpp` | **PC D3D12 → Vulkan 层**（资源、管线、描述符、`Map`、present 接线） |
| `runtime/src/vulkan_replay.h` | 把录下来的 D3D12 命令翻成 Vulkan；**draw 的日志在这里** |
| `runtime/src/vulkan_present.h` | 呈现：swapchain、plane、`LEONIDA_CAPTURE` |
| `runtime/src/shader_library.h` | 语料 index 的读取与查找（FSSM / FSPX kind-6+kind-0） |
| `runtime/src/descriptor_table.h` | 着色器用的 **8-dword FNV-1a 描述符哈希表**（set 0 binding 2） |
| `runtime/src/guest_memory.h` | `copy_committed` / `write_committed`（**每次写都 `VirtualQuery`，这是稳态 80% 单核的来源，是个已知可优化点**） |
| `tools/rdna2spirv.py` | RDNA2 汇编 → SPIR-V 的翻译器（54,820 个程序） |
| `tools/fspx_unpack.py` | FSPX payload 拆包 + 反汇编（**kind-6 头部 3 个 dword 反汇编错位，已知未修**） |

---

## 12. 交接时的工作区状态

- **分支 `main`，本地提交从未 push**（`git status` 干净；只有 `tmp/`、`.idea/` 未跟踪）。
- 本轮及之前相关提交（新→旧）：
  `4901d732f` 交接文档 · `b7094ccc6` 上游对比 · `3e3abec88` 纹理 Map ·
  `b7feb7733` 稳态表 · `238af8d24` 稳态画像+slot81 · `4b54da9a9` compute 锁 ·
  `0a506b979` 图形锁回滚 · `800b4ba15` 图形 PSO 缓存 · `7b921a0ba` 驱动自旋文档 ·
  `da3d6a712` 采样器+compute 缓存 · `8cf1a577d` slot 67 不再阻断 · `9a93cb233` 描述符链路接通
- **`XS_EMPTY_CS` 默认关**：不开的话 slot 67 每帧失败（0x80070057），这是已知的**语料缺口**
  （缺的那个程序是个 76 字节的 FLT_MAX 填充内核，反汇编头部错位导致翻译器拒绝）。
- 语料没有被本轮改动（sha256 未变）。

---

## 13. 下一个人最该做的三件事（按顺序）

1. **证明那 4 个 blit 画进了哪张图、present 呈现的是哪张图**（§1 的顺序）。
   如果同一张 ⇒ 查 `VS_Blit` 的 input layout（**stride 24、颜色在 +12**）。
2. 如果 input layout 是对的，就把 `LEONIDA_THREAD_MAP=30` 开久一点，盯稳态那两个线程
   （`list_slot<72>` 的 `write_committed` 80%，和 `game+0x2481f73` 56%），
   配合 REA snapshot 反汇编它们，弄清标题在轮询什么。
3. 修 `tools/fspx_unpack.py` 对 kind-6 头部 3 个 dword 的反汇编（有 prologue 没跳过），
   补上语料缺口，然后关掉 `XS_EMPTY_CS`。

**别做的**：再碰描述符链路、再追 amdvlk 的 CPU、再等时间、再去上游找答案。
