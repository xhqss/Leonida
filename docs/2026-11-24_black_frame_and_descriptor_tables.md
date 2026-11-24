# 黑屏追到描述符表：这一轮做了什么、坑在哪

2026-11-24 21:00–22:30 这一段。前一份记录是 `2026-11-24_upstream_merge_and_white_window.md`（结论：白屏是
PresentX 被拒绝）。白屏解决之后画面是**黑屏**：标题能启动、能跑、PresentX 成功、帧率正常，但送显的每一帧全零。
本文记录这一轮把它追到了哪里，以及后来者要继续时应该/不应该怎么走。

**与前一份记录的关系：** 那份的 §8 结束时卡在"guest 顶点缓冲被图层当作 `unknown resource`（提交失败）"和
"每帧 list 不设 viewport/scissor（不画）"。这轮这两项已经不再出现：顶点/索引视图走 guest 窗口，
每次 draw 的 viewport/scissor 都是 `0,0 3840x2160` / `0,0..3840,2160`，默认跑 **0 次提交失败、约 57 presents**。
所以下面的黑屏是**更靠后**的一层：命令提交成功、PresentX 成功，但送显的纹理内容本身是 0。

**一句话结论：** 送显的那张图由每帧最后一个"合成"draw 写；该 draw 的着色器从一张 root descriptor table 里读
V#/T#，而**这张表的 VA 从来没有被翻译**（root descriptor setter 翻译了，table 没有），于是着色器把
`heap_base + 标题VA` 算成 arena 里 194 MB 处的一片零，拿到的描述符全是 0 —— 顶点位置退化、采样全黑。
把表翻译成标题页窗口之后，着色器第一次读到了真正的 console 描述符，但紧接着某处越界读导致
`VK_ERROR_DEVICE_LOST`；所以这个修法暂时做成 opt-in（`LEONIDA_XS_TABLE_VA`），默认行为保持稳定。

---

## 1. 上游状态与本轮落地的补丁

`origin/main` 这轮前进了 47 个提交（`51f316092..9600cf0c6`）；本地 91 ahead / 1162 behind。
整体合并仍然不值得（`git merge-tree --write-tree HEAD origin/main` = **23 个冲突文件**，且上游连 `--run`
都没跑过，停在 `docs/retail_boot_chain.md` 的 stop 13）。按需摘取、逐个验证后落了 8 个上游提交：

```
338811fdc  SetThreadGroupAffinity 服务零售 exe 的导入
0e36ba6d9  gameinput: GameInput.dll ordinal 1（零售 exe 的 GameInputCreate）
ef5406cf1  spirv_f16_halves: 链接快路径 / Private 装饰 / location 预算
ace0cfb1f  拒绝 workgroup 内存超过 maxMeshSharedMemorySize 的 mesh PSO
18e0937fa  store: 离线 *Async 槽用 E_FAIL 起停 XAsyncBlock
6ada8151b  pe_loader: 拒绝头里的 IAT/TLS 索引
86ff8ca1a  fs/dstorage: ReadFile 到首次触碰的标题内存先提交页面再重读
b1f133de0  （上游同批 fixup）#6942 测试需要 fcntl/io
45656426a  （本地写）process_cmdline_test: 探针后把线程亲和性放回
```

方法：`git format-patch -1 <sha> -o tmp/upstream-patches` → `git apply --check` 验证 → `git am`。
**注意：** merge 提交（例如 `29f66ba0f`）的 `format-patch` 输出是**空文件**，`git apply --check` 对空补丁
返回成功 —— 上一轮那个 "clean=True" 是假阳性，别看这个信号。

构建：`tmp\build-gta6rt.cmd gta6rt`。完整 `ctest` 回到既有基线：**只剩 6 个本会话之前就失败的**
（`run_module_names`、`d3d12_com` SEGFAULT、`d3d12_vulkan_device`、`d3d12_depth_resolve`、
`d3d12_pipeline_library_host`、`dxgi_swapchain`）；`d3d12_texture_tiles` 在 `-j8` 下偶发失败，单独跑通过。

## 2. 真正落地的修复：图形根参数打包位置（base s8）

**症状：** 标题的 VS/PS 读 user SGPR，但读到的根参数全是 0。

**证据：**
- 语料 `index.json` 的 `user_sgpr_reads`：最低的指针 SGPR 在 1,660/2,236 个 VS、9,830/12,101 个 PS 里是 **s8**；
- 层里的图形绑定点当时用 `rt::rootsig::pack()`：**从 user dword 0 开始、每个 descriptor table 占 2 个 dword**；
- `LEONIDA_LOG_USERDATA` 实测：根 SRV 的 VA 落在 user **dword 0**，而着色器读 **s8/s9** → 读到 0。

**修法（`d124154dc`）：** `CreateRootSignature` 的图形布局改用 `rt::rootsig::pack_stage(Stage::Vertex)`
（base s8、每个 table 1 个 dword）；放不下时回退旧的 root-order 打包并打印一行说明；
`RootArgumentLayout::graphics_console` 记住用的是哪套，写地址时按布局决定 table 写 1 或 2 个 dword。
mesh 阶段（base s16）仍未覆盖，需要翻译器侧的 per-stage user-data 窗口。

**验证：** `LEONIDA_LOG_USERDATA` 的 s8 变成 `308aff80`（窗口编码）；`LEONIDA_LOG_ROOTSIG` 打印
`graphics layout console (base s8, one dword per table)`；`ctest` 无回归。

## 3. 每帧到底有哪些 draw（认知更新）

用 `LEONIDA_LOG_REPLAY_DRAW`（画时打 pipeline 句柄）+ `LEONIDA_LOG_DRAW_PIPELINE`（建管线时把句柄映射到
阶段名和模块 FNV 摘要）+ `LEONIDA_LOG_PRESENT`（present 的资源 id）对齐后，稳定阶段的每帧是：

| draw | 着色器（语料名） | 渲染目标 |
| --- | --- | --- |
| #1,#2 | `vs-01ac7810dcabd62a` + `ps-452212e59d983c3b`（各 0 binding，手工 fetch） | 4K 中间纹理 A |
| #3 | `VS_Blit_6e5520d1_Wrapped` + `PS_BlitColour_6e5520d1_Wrapped` | A |
| **#4** | **`)sga_dr_vs_pos_color_tex_c32788bc_Wrapped` + `default_ps_tex_c32788bc_Wrapped`（合成）** | **送显纹理 P（双缓冲两块交替）** |

同一次运行里 draw #4 的 colour target id 与 `LEONIDA_LOG_PRESENT` 的 present 资源 id **完全一致**。

**由此修正一条旧结论：** `LEONIDA_DRAW_TINT=both` 让画面变洋红，**不代表那几个场景 draw 覆盖了屏幕** ——
它把合成 draw 的 VS/PS 也换成了内置调试模块。判断"某个 draw 是否覆盖"必须针对那一条 draw 单独做实验。

## 4. 黑屏定位过程（可复现的实验顺序）

1. `LEONIDA_LOG_DRAW_PIPELINE` 证明"画的那条管线"就是用 `LEONIDA_SHADERS` 下那份 `.spv` 建的：
   未改时摘要 `vs=62345f74 ps=408c1865`（blit `a4265d09`/`b629398a`），与 `tmp/fnv.py` 算出的文件摘要一致；
   `LEONIDA_LOG_PIPELINE_FAIL` 计数为 **0**（没有管线被驱动拒绝）。
2. 把合成 VS 的**最后一次** `OpStore %out_pos` 换成"由 `gl_VertexIndex` 生成的全屏三角形"，同时把合成 PS
   尾部强制洋红（接口不动，只改输出），用 `spirv-as` 重新汇编、`spirv-val` 校验、替换语料文件后跑：
   捕获帧 `min=0 max=255 nonzero=66.67%`（洋红铺满）。**draw→管线→RTV→present 整条链是通的**，
   黑屏来自合成 VS 自己算出的位置。
3. 表里有什么：`setgraphicsrootdescriptortable` 的表 VA 是**标题自己的页**（`copy_committed` 能读出
   真正的 console 描述符）；而在 arena 的同一偏移处，`LEONIDA_DUMP_TABLE` 显示**全零**。
4. 结论：slot 36（descriptor table）的 VA **没有翻译**；root descriptor setter（slot 41–46）有
   `shader_address()` 翻译，table 没有。着色器按 `heap_base + 标题VA` 读 → 读到 arena 的零。

## 5. opt-in 修法 `LEONIDA_XS_TABLE_VA`

打开后 slot 36 对表 VA 做与 root descriptor 相同的翻译：`0xb94ef00 -> 0x30d0ef00`（标题页窗口编码），
`LEONIDA_LOG_ROOT` 打出的表内容从全零变成真实描述符。标题因此走得更远（音频初始化、队列 slot 31、
真正开始用描述符渲染），但随即：

```
[rt] d3d12: ExecuteCommandLists failed: unsupported; nothing was submitted
[rt] d3d12: the replay stopped at command 0 (TransitionBarrier, kind 0)   ← 假象
[rt] d3d12: present failed (VkResult -4)                                 ← VK_ERROR_DEVICE_LOST
```

`unsupported` 是 device lost 之后队列的拒绝（`vulkan_backend.h` 的 `is_device_lost()` 分支），
不是"命令不支持"。最可能的越界来源：合成 VS 从真实 V# 里取 base 做 `heap_base + base` 访问顶点，
而 base 可能仍是标题 VA（48 位截断/越界，见 `docs/d3d12_vulkan.md` §3.3 的 V# 说明）。
所以默认**关闭**该翻译（默认 run 仍是 57 presents、0 错误）；打开只在做这个排查时。

## 6. 新增探针（全部环境变量开关，默认关）

| 变量 | 打印什么 | 用途 |
| --- | --- | --- |
| `LEONIDA_LOG_DRAW_PIPELINE` | 建管线时记下"句柄→阶段名+每阶段模块 FNV"；draw 时打印该注记 | 证明画的是哪条管线、哪份模块字节 |
| `LEONIDA_LOG_PRESENT` | present 的 source 资源 id/尺寸/格式 | 与 draw 的 colour target id 对齐 |
| `LEONIDA_LOG_DESC_WRITE` | 每个 T# 写到哪里、是否在 arena 内 | 判断描述符写侧是否正常 |
| `LEONIDA_DUMP_TABLE` | root table VA 处的 arena 字节（`runtime/src/arena_window.h`） | 看着色器会读到什么 |
| `LEONIDA_LOG_MODULE` | 每个阶段加载到的模块 sha256 前 16 位 | 判断打补丁的语料是否真的被读 |
| `LEONIDA_LOG_PIPELINE_FAIL` | 驱动拒绝的管线（带阶段名），前 5 条 + 每 5000 条 | 排除"管线建不出来" |
| `LEONIDA_LOG_UNSUPPORTED` | replay 卡在第几条命令、什么类型（`runtime/src/replay_progress.h`） | 区分"命令不支持"与"device lost" |
| `LEONIDA_LOG_ROOT` / `LEONIDA_LOG_VB` | root descriptor 24 条翻译（前 2 条带 32 dword）、VBV 前 40 条带 16 dword | 看 VA 翻译与内存内容 |
| `LEONIDA_XS_TABLE_VA` | （行为开关）翻译 descriptor table VA | 第 5 节的实验 |

## 7. 坑（这一轮踩过的，按重要性）

1. **只改头文件不会触发 ninja 重编。** `runtime/src/vulkan_replay.h` 改完后 `cmake --build` 报
   `ninja: no work to do`，exe 时间戳没变，日志里的新打印永远不出现。必须 `touch` 一个包含它的 `.cpp`
   （本轮用的是 `runtime/shims/d3d12_vulkan_device.cpp`）再构建，否则会得出"探针没生效"的错误结论。
2. **临时改语料 `.spv` 必须备份并在实验后按 sha256 还原。** 本轮动过 6 个文件：
   `vs-01ac7810dcabd62a`、`ps-452212e59d983c3b`、`VS_Blit_6e5520d1_Wrapped`、
   `PS_BlitColour_6e5520d1_Wrapped`、`default_ps_tex_c32788bc_Wrapped`、
   `)sga_dr_vs_pos_color_tex_c32788bc_Wrapped`。每个都有 `tmp/*.orig.spv` 备份，还原后逐个比对；
   当前语料 0 个文件与备份不一致。
3. **`LEONIDA_DRAW_TINT=both` 不能用来判断"某个 draw 是否覆盖"** —— 它替换所有图形管线的 VS/PS。
   `=ps` 时若 VS 不覆盖仍会黑；`=vs` 会因真实 PS 的输入接口没人写而导致管线创建失败（只有 4 presents）。
4. **在入口块里写 `gl_Position` 会被程序后面的 `OpStore %out_pos` 覆盖。** 第一次"强制全屏三角形"实验
   因此得出假阴性；正确做法是替换**最后一次**位置 store（`tmp/patch_vs_tail.py`）。
5. **device lost 之后 `ExecuteCommandLists` 一律报 `unsupported`。** 不要据此判断"某命令不支持"；
   看 `present failed (VkResult -4)`，或用 `LEONIDA_LOG_UNSUPPORTED` 看具体卡点。
6. **本会话 shell 的进程亲和性是 `0xffff0000`（CPU 16–31）**，于是 `process_cmdline` 里
   "进程掩码之外的核会被 host 拒绝"这个假设不成立（`SetThreadGroupAffinity` 会接受），测试失败。
   已按事实修正测试（`45656426a`）；换台机器可能不复现。
7. **上游 merge 提交的 `format-patch` 是空的**，`git apply --check` 会误报 clean（见第 1 节）。
8. 网络只用于 `git fetch`（直连 GitHub 三次失败后按用户指示用 `127.0.0.1:7897` 代理，`-c http.proxy`
   逐命令传，未改任何全局配置）。**不 push**：所有提交都只在本地。

## 8. 当前状态与复现命令

- 本地提交（未推送）：`3c64baeef`（本轮探针 + opt-in 表翻译）、`86cf89079`、`fbdf2b8b9`、`d124154dc`、
  `45656426a`，以及第 1 节的 8 个上游补丁。
- 稳定跑法（默认，黑屏但无错误）：`tmp\run-watch.ps1 -Seconds 40 -Log tmp\run.log`
  → 约 50–70 presents、0 个 failed。
- 探查跑法：
  ```powershell
  $env:LEONIDA_XS_TABLE_VA='1'
  $env:LEONIDA_LOG_DRAW_PIPELINE='1'
  $env:LEONIDA_LOG_REPLAY_DRAW='1'
  $env:LEONIDA_LOG_PRESENT='1'
  .\tmp\run-watch.ps1 -Seconds 30 -Log tmp\black.log
  ```
- 重新构建：`tmp\build-gta6rt.cmd gta6rt`（构建前必须 `Stop-Process gta6rt`，否则 LNK1168）。

## 9. 下一步

1. 打开 `LEONIDA_XS_TABLE_VA=1`，抓那个越界读：优先看合成 VS 读到的 V# 8 个 dword（`LEONIDA_LOG_ROOT`
   已经能打表内容），把 `heap_base + base` 与 `stride/num_records` 代进去算它要访问的地址，
   判断是 base 未翻译、还是 bounds 失效导致的越界。
2. 若确认是 V# base 仍是标题 VA：在描述符写入/翻译路径把 V# base 字段改成 `heap_base` 相对值
   （或者让 arena 覆盖它），也就是把 `docs/d3d12_vulkan.md` §3.3 的"V# 必须落在 arena 范围"真正落实。
3. 每步之后跑完整 `ctest`，保持"只剩那 6 个既有失败"的基线；语料临时改动一律按第 7 节第 2 条处理。

---

# 追加：第二轮（2026-11-24 22:30–23:15）—— 越界读抓到了，表内容对不上

本节覆盖第 9 节的三步。**结论先行：越界读已用 `VK_EXT_device_fault` 抓到，是"标题写进表里的 16 字节
条目被着色器当 V# 解出来、base 落在 arena 外"；把这类条目清零后设备不再丢，但画面仍是黑的 —— 卡的
是"标题写的表条目格式"和"着色器期望的 V# 格式"不一致。**

## 1. 新工具：`VK_EXT_device_fault`（`LEONIDA_DEVICE_FAULT=1`）

amdvlk 支持 `VK_EXT_device_fault`（`deviceFault=true`，`deviceFaultVendorBinary=false`）。设备创建时
按需打开扩展 + `VkPhysicalDeviceFaultFeaturesEXT`；`VulkanContext::mark_device_lost()` 里调用
`vkGetDeviceFaultInfoEXT` 打印一次故障地址，并同时打印 `heap_base` 和"相对 heap_base 的 console VA"。
抓到的两类故障（两次运行）：

```
device fault: GPU page fault detected
  fault address 0: 0x1520e88e000 (read invalid, precision 0x1000); heap_base 0x202e40000; console VA 0x1500ba4e000
  fault address 0: 0x80a02e40000 (read invalid, precision 0x1000); heap_base 0x202e40000; console VA 0x80800000000
```

即着色器算出的读地址 = `heap_base + <某个 48 位 base>`，那个 base 就是表里条目解码出来的 V# base。
`precision 0x1000` 表示真实地址在 `[reported, reported+0x1000)`，所以第二个故障的 base 是
`0x80800000004`，第一个是 `0x1500ba4ef00`。

## 2. 关键数字：arena 只有 **256 MB**

`LEONIDA_LOG_SANITIZE` 会打印 `arena 0x10000000`。所以判断"V# 是否可能有效"的判据很硬：
`base >= 0x10000000` 或 `base + records > 0x10000000` 一定越界。这一条以后可以直接写进校验。

## 3. 表到底存在哪：三个位置都不一样

`LEONIDA_LOG_TABLE=1` 对一个根表同时打印三个来源（前 0x100 字节 + 按 V#/T# 解码 + 越界标记）：

| 位置 | 内容 |
| --- | --- |
| **arena 的同一偏移**（`heap_base + table_va`，着色器默认读的） | **全零** |
| **堆的 XMem backing**（标题自己的堆内存，`custom_cpu_start + 偏移`） | **全零** |
| **宿主地址 = table VA 数值处**（`copy_committed(table_va)`） | **有数据**（形状像描述符） |

也就是说：标题把描述符表写在"它自己拿到的 GPU handle 数值当宿主地址"的那块内存里（console 统一内存
的假设），而层把 `Create*View` 写的描述符放在 arena 的堆里。**两边从来不是同一块内存。**
所以：

- 默认（不翻译表 VA）：着色器读 arena → 全零 → V# 全零 → 黑屏、不崩；
- `LEONIDA_XS_TABLE_VA=1`：着色器读"窗口拷贝"（标题内存）→ 读到真数据，但其中有些条目解出来是
  越界 base → 设备丢失。

## 4. `LEONIDA_XS_TABLE_FIX=1`：带消毒的表翻译

`guest_translate()` 增加 `sanitize` 参数：每 4 MiB 窗口拷贝（本来就每次调用都刷新）之后，扫描前
64 KiB 的 16 字节条目，把解出来 `base >= arena` 或 `base + records > arena` 的 V#（以及越界 T#）
清零（load 返回 0，等价于 size 为 0 的缓冲区）。

结果：**120 秒稳定，0 个设备丢失，3 帧照常抓到 —— 但帧仍是全黑（mean rgb 0.00 0.00 0.00）**。
所以"设备丢失"这个次生问题已经压住，黑屏是描述符格式问题。

## 5. draw → 表 的对应关系（这次拿到了）

`LEONIDA_LOG_USERDATA=1` 扩到前 16 个 draw、每个 16 个 user dword。和 `LEONIDA_LOG_TABLE` 对起来：

| draw | 管线 | `s8`（表 VA，已翻译） | 标题表 VA |
| --- | --- | --- | --- |
| #1,#2 | `vs-01ac7810…` + `ps-452212…` | `0x2f3eff80` / `0x2f40ff80` | 根 descriptor（不是表） |
| #3（blit） | `VS_Blit…` + `PS_BlitColour…` | `0x2f96ef00` | `0xb96ef00` |
| **#4（合成，写送显纹理）** | **`)sga_dr_vs_pos_color_tex…` + `default_ps_tex…`** | **`0x2f96dea0`** | **`0xb96dea0`** |

## 6. 合成表的内容对不上 gfx10 V#

合成 draw 用的表（标题 VA `0xb96dea0`）头 8 个 dword：

```
00000002 00000844 00000bc6 00210440 | 00000041 00000300 00000048 00000000
```

按文档 §3.5 的 gfx10 V# 解码（`base = (w1 & 0xffff) << 32 | (w0 & ~3)`）得到
`base = 0x84400000002`，远在 256 MB 之外；按 T# 解码 `TYPE=0`（不是图像）。别的表里还有
`08ed1e20 00000000 08ed1e20 00000000`（base 与 num_records 相同）、
`00000004 00000808 ffffffff 00000000`（`0x808<<32` 正是第二个故障的 base）这类形状。
**结论：标题写进表里的不是（至少不全是）gfx10 的 16 字节 V#。** 这也解释了 `LEONIDA_LOG_SANITIZE`
为什么把整表清零。

## 7. 这一轮踩到的坑（新增，按重要性）

1. **只改头文件 + 构建 = amdvlk 里随机崩溃（3–5 秒）**。`runtime/src/vulkan_backend.h` 里加一个
   `VulkanDeviceFunctions` 成员后，ninja 只重编了 `d3d12_vulkan_device.cpp`（其他 TU 的 depfile 里
   没有这个头），剩下的 TU 用旧结构体布局 → ODR 破坏 → amdvlk 在 `CreateFence` 附近
   `c0000005`（偏移 0x1ac64c / 0x160169 随机）。**症状是"游戏 3 秒退出"，不是报错**；对照实验：
   `git stash` + 重建（全量）后就正常。**规避：动任何 header 之后，先
   `Get-ChildItem runtime\shims,runtime\src -Filter *.cpp | % { $_.LastWriteTime = Get-Date }` 再构建。**
2. `is_committed()` 会骗人：宿主地址 0xb800000 恰好是已提交的标题内存，于是
   `guest_translate(0xb94ef00)` 会把"arena 偏移"当成"标题内存"去拷贝。判断一个 VA 是哪种，不能只看
   `is_committed`，要先用 heap 的 GPU handle 范围（`LEONIDA_XS_GPU_HANDLE` 注册的那张表）判一次。
3. `raw_heap_get_gpu_handle` 用"返回值 + 出参"两种约定都试，直接只取返回值会拿到
   `0x1346cf778` 这种栈地址；其实层里堆的 GPU handle 就等于 `real_cpu_start - arena_host_base`，
   不用调 slot 10。
4. 设备丢失后 probe 仍在跑，别把"丢了之后"的日志当正常数据。
5. `LEONIDA_LOG_TABLE` 会很啰嗦（每张表 17 行），跑探查时记得和其它开关一起只开短时间。

## 8. 当前状态

- 默认跑法仍然稳定黑屏（无设备丢失）；`LEONIDA_XS_TABLE_FIX=1` 也稳定黑屏。
- 新增开关：`LEONIDA_DEVICE_FAULT`、`LEONIDA_LOG_TABLE`、`LEONIDA_LOG_SANITIZE`、
  `LEONIDA_XS_GPU_HANDLE`、`LEONIDA_XS_TABLE_FIX`（全部默认关）；`LEONIDA_LOG_USERDATA` 扩到
  16 个 draw × 16 dword。
- 二进制里所有新增行为都在 env 开关后面，默认路径不变。

## 9. 下一步（下一位接手者）

1. **搞清标题写表的格式**。三条路，按推荐顺序：
   a. 反查写表代码：对 0xb96dea0 这个地址下页级写监视（`VirtualProtect` + 异常/写时复制很难，或
      用 `cdb` 的 `ba w` 断点；本机目前没有 cdb，得先找）；
   b. 用 xcl / VIctory 的实现对照：它们也处理 console 描述符表，看它们怎么解 16 字节条目
      （`crates/xcl/src/hle/d3d12x/`、`crates/victory-shader/src/resource.rs`）；
   c. 从语料侧反推：`tools/rdna2spirv.py` 里 buffer_load 的翻译假定 V# 是 gfx10 布局；找一个**已知能
      正常出画**的 draw（比如 blit 之前那两个 fetch draw）看它的表条目是不是同一个格式 —— 如果
      那两个 draw 的表也是这种形状却能出画，说明格式没错、是合成 draw 的绑定参数不同。
2. 若确认条目是 32 字节（不是 16），把 `sanitize_descriptor_table` 的步长和着色器读取偏移一起改，
   并用 `LEONIDA_LOG_TABLE` 的 arena/backing 三份对照验证。
3. 真正的架构解（比逐条修更彻底）：让 shader-visible heap 在层里只有一个存储 —— 让
   `GetCPUDescriptorHandleForHeapStart` 和 `GetGPUDescriptorHandleForHeapStart` 返回**同一个**标题
   XMem backing 地址（console 统一内存），`Create*View` 也直接写到 backing，表 VA 就是 backing 地址，
   着色器用 guest window 读它。这样"标题自己写的"和"层写的"描述符天然是同一块内存。
   （注意 RTV/DSV 等非 shader-visible heap 不能跟着改，它们的数据是层自己读的。）

---

# 追加：第三轮（2026-11-24 23:15–23:30）—— 表条目不是 gfx10 V#

## 1. 这一轮确认的事

- 合成 VS 的位置 = `s16..s27` 的四行矩阵 × 顶点寄存器 `v4..v7`（反汇编 825-1218 行）。矩阵不是根常量，
  而是着色器**从描述符表里那个 V# 指向的缓冲区自己读出来的**：prologue 依次从 `V#base + 0x00/0x10/0x20/0x30`
  读 4×4 个 dword 存进 `s16..s31`，每一步都带 `records/stride` 边界检查。
- 表条目的 base 是**标题自己的宿主地址**（`is_committed(base)` 为真）：把它们 `guest_translate` 成窗口后，
  层里出现了 0x8cc00000 / 0xb5000000 / 0x30000000 / 0xffc00000 这些窗口（4 MiB 一个）。
- 但条目里 `base` 后面的 dword（着色器当 `NUM_RECORDS` 用）就是**同一个指针值**：表内容形如
  `{ptr, 0, ptr, 0}`（`ptr` 在 0x8e..-0xc2.. 之间重复），不是 gfx10 的
  `{base_lo, base_hi|stride, num_records, sel}`。把 base 改成窗口地址后，`records` 变成天文数字，
  着色器/计算着色器按它循环 → **GPU 挂住**（mag2 运行卡在 `queue slot 31` 后的提交，80 秒没再出现 present）。
- 用"合成 PS 强制洋红"做判定：`tmp/composite-ps-magenta.spv`（校验通过，只在原 PS 尾部多一条
  `OpStore %3 %49`，%49=(1,0,1,1)）替换语料后，帧仍是 **min=0 max=0**。即：合成 draw 一个像素都没写出来，
  VS 算出的位置仍然退化 —— 与"矩阵读成 0"一致。**语料已按 sha256 还原**
  （`default_ps_tex_c32788bc_Wrapped.spv` = `E231E529…BAA3`）。

## 2. 新结论

1. 表条目**不是** gfx10 16 字节 V#。至少对合成 draw，1) 16 字节窗口解出的 `base` 是标题宿主地址（合理），
   2) 紧随其后的 dword 是同一个指针（被当成 `NUM_RECORDS` 完全不讲道理），3) 相邻条目里多数是
   `{ptr, ptr}` 对、少数是 `{ptr1, ptr2}` 两个不同指针——更像**指针数组/范围对**，不是硬件描述符。
2. 因此 `LEONIDA_XS_TABLE_FIX` 现在的"改写 base + 保留 records"会制造 GPU 挂死。**该开关目前只适合
   配合更保守的策略使用**（例如只改 base、把 records 一并改成一个小值，或者干脆只读前 64 字节）。
   默认关闭，不影响稳定跑法。
3. `LEONIDA_XS_TABLE_STRICT=1` 会让 sanitize 按 `base+records` 判越界；第三轮的
   `LEONIDA_XS_TABLE_FIX` 已改为"base 是标题内存就翻译，否则按 arena 判"，这一条需要下一步重新设计。

## 3. 下一步（留给下一位）

1. **先弄清指针表的语义**，不要再猜 gfx10：表里 `{ptr, ptr}` 的对很可能是引擎自己的"资源范围对"
   （begin/end）或"句柄+映射地址"。最直接的办法仍是给表页下写监视（VEH + PAGE_GUARD）抓写它的 RIP，
   或者从 `tools/rdna2spirv.py` 侧确认这个 draw 的 VS 真的应当从表里读 V#（也可能是翻译器多了/少了一层
   间接）。
2. 在弄清之前，`LEONIDA_XS_TABLE_FIX` 先只做"窗口拷贝 + base 翻译 + records 归零（或 clamp 到 64）"，
   保证不挂 GPU；再用"洋红 PS"判定位置是否终于非退化。
3. 如果确认是翻译器问题（console shader 其实读的是 8 字节指针而不是 V#），改法在
   `tools/rdna2spirv.py` 的 buffer op 翻译处，语料需要按 `docs/shader_library.md` 的流程重生成，
   那是比层里打补丁更根本的修法。
4. 恢复语料的检查命令（每轮实验后都要跑）：
   ```powershell
   (Get-FileHash 'E:\temp\wait_work\leonida-work\shaders\spirv-561b65a\default_ps_tex_c32788bc_Wrapped.spv').Hash
   # 期望 E231E529E80341216949C62E8127163AD3F7EFDBC8DA29E77247114F41A0BAA3
   ```

---

# 追加：第四轮（2026-11-24 23:30–23:40）—— 把表条目当"指针再解一层"，仍不满足合成 draw

## 1. 这轮做的实验

`LEONIDA_XS_TABLE_REWRITE=1` 里给 `NUM_RECORDS` 加了 clamp（重写后的 records 一律 ≤ 4096），
这样"改写 base 但 records 是天文数字"的 GPU 挂死消失：带洋红 PS 的 90 秒跑完整抓了 2 帧、不再卡住。
于是又加了 `LEONIDA_XS_TABLE_INDIRECT=1`：表条目 base 处的标题内存是 `{ptr, 0, ptr, 0}` 形状，
就再解一层，把 `*(base)` 当真正的数据地址（再 `guest_translate` 成窗口）。

结果（90 s，洋红 PS）：

```
present: first frame shown ... capture 1
frame_00001.ppm: min=0 max=0 non-zero 0/15770652   ← 仍是全黑
rewrite +0x0: 6d03c230 00000000 00001000 00000000 from 0x90d2110; data 0993c230 00000000 0c497e10 00000000
```

即：链条 `表条目(0x90d2110) → *(0x90d2110)=0x993c230 → 窗口 0x6d03c230` 走通了、不再挂，
但**合成 draw 仍然一个像素都不写**（洋红 PS 也一样）。说明位置还是退化，或者矩阵根本不在这一层。

## 2. 现状判断（下一轮的起点）

- `LEONIDA_XS_TABLE_FIX` + `LEONIDA_XS_TABLE_REWRITE` + `LEONIDA_XS_TABLE_INDIRECT` 现在**稳定不挂**，
  但画面仍黑。这条"猜层级"的路已经走到底：再猜下去只是在赌，收益低。
- 真正的判据还是缺"谁写这张表、写的是什么"。下一步优先级：
  1. **写监视**：给表页装 `PAGE_GUARD`（VEH 抓 `EXCEPTION_GUARD_PAGE`，记 RIP + 写入方式），
     拿到写表代码的 RIP→RVA，用 `dumpbin /DISASM` 或项目里的 `tmp/dis.py`（需 capstone，本机现装无）
     反汇编，看它写的到底是哪种描述符。
  2. 若确认为"引擎自定义的指针表"（很可能）：修法不在层，而在 `tools/rdna2spirv.py` 的 buffer op
     翻译——console shader 的 `s_load_dwordx4` 也许应先解一层指针；改翻译器 + 按 `docs/shader_library.md`
     重生成语料，再用洋红 PS 判定。
  3. 层侧只保留"窗口拷贝 + base 翻译 + records clamp"的稳定版本，别再叠加猜测性的层级。
- 稳定跑法不变（默认无设备丢失、无挂死）；语料已还原并核验（`E231E529…BAA3`）。

## 3. 从 xcl 抄到的重要事实（下一轮直接用）

读了 `E:\temp\wait_work\xcl\crates\xcl\src\hle\d3d12x\console_heap.rs` 的头注释，它把 console 的真实约定写清楚了：

> On the console a descriptor is GPU-readable memory: **32 bytes per CBV/SRV/UAV slot (T# / V#)**, 16 per
> sampler (S#), and a descriptor table in a root argument is a **32-bit GPU address** (shaders load it with the
> high half 0). … XCL gives console code heaps whose slots live in **unified memory** (so the table addresses
> console code computes from the heap start work as they are in the recompiler's `mem` model) …

对我们有三点直接用处：

1. **槽位是 32 字节**，不是 16。合成 VS 读 `s8+0` 和 `s8+0x40` 正好是 slot 0 和 slot 2 —— 说明它的根签名有两个
   范围、各 1 个描述符，位置在 slot0 / slot2 的**前 16 字节**（gfx10 V#）。
2. **表地址是 32 位 GPU 地址，统一内存**：所以标题把 `base` 写成宿主地址是对的，我们 `guest_translate` 的方向
   也对；问题只在**槽位内容**。
3. xcl 的结论是"console 标题自己把描述符当内存复制"（inline `CopyDescriptorsSimple`），所以表里应当是
   **硬件 V#**。但实测 slot0 的前 16 字节是 `{0x8ed1e20, 0, 0x8ed1e20, 0}`：base 合理、`dst_sel/format` 全 0、
   `NUM_RECORDS` 等于 base —— 这提示我们看到的可能是**比 32 字节槽更靠前的偏移**（表 VA 不是 slot 起点），
   或者 slot 内 V# 在**后 16 字节**。下一轮先把表 VA 前后各扫 0x40 字节、按 16/32 两种步长解码，确认 V# 到底在哪。

---

# 追加：第五轮（2026-11-24 23:36–23:42）—— 指针链在打转，值全在 arena 范围

## 1. 三层层级 dump 的结果

给 `rewrite_table_descriptors` 的日志加了三级指针链（`[0]` = 表条目 base 处，`[1] = *(base)`，`[2] = *(*(base))`）。
合成 draw 那条的输出：

```
rewrite +0x0: 6cc3c0d0 00000000 00001000 00000000 from 0x90d1e20
  [0] 0x90d1e20: 0993c0d0 00000000 0c497cb0 00000000
  [1] 0x993c0d0: 0c0ea590 00000000 090d1e20 00000000
  [2] 0xc0ea590: 0993c230 00000000 0993c0d0 00000000
```

**三个层级都是同一形状（{ptr,0,ptr,0}）并且互相指来指去**（0x993c0d0 里又指回 0x90d1e20 一类），
所以"再解一层就是矩阵"的假设是错的——那条链是某种节点/描述结构，不是数据。

## 2. 一个更可能的解释（下一轮的验证点）

这些值**全部落在 256 MB 以内**（0x90d1e20 = 151 MB、0x993c0d0 = 160 MB、0xc0ea590 = 202 MB…），
正好是 **arena（console VA）范围**。而 `arena_host_base` 读出来的arena 内容是全零——文档 §3.3/§3.4 早写过
"default-heap 的宿主映射对 GPU 写入是过时的（stale）"。标题很可能在**GPU 侧**填这些缓冲区（拷贝/计算），
所以：

- 表条目很可能**本来就是合法的 gfx10 V#**，`base` 是 arena VA（标题的 GPU 缓冲区）；
- 我们读 host 映射看到全零，是**看不到 GPU 写入**，并不说明着色器读到零；
- 之前"`NUM_RECORDS` 等于 base"很可能只是**表 VA 没有对齐到 slot 起点**（32 字节槽内的偏移差 0x10），
  把 V# 的前后字段错位读了。

如果这个解释成立，那么"改写 base / 解指针 / 截断 records"这些实验方向**会把本来正确的描述符改坏**，
这也解释了为什么它们从没让画面变好。

## 3. 下一轮（很具体，成本低）

1. 写一个只读探针：在 slot 36 处把表 VA **从 -0x40 到 +0x80** 每 4 字节滑一个 16 字节窗口，按 gfx10 V# 解码，
   用"`dst_sel` 四个 3-bit 字段都 ≤ 5、`FORMAT` 在 1..127、`TYPE == 0`、`BASE` 落在 arena 内"打分，
   打印得分最高的偏移。合成 draw 的表（第 4 个 slot-36 调用）应该能直接把 V# 的**真实偏移**顶出来。
2. 找到真实偏移后再看 `NUM_RECORDS`/`STRIDE` 是否合理；如果合理，就**撤掉所有改写/截断/解指针**，
   只保留"表 VA 窗口拷贝"（`LEONIDA_XS_TABLE_FIX` 的原始形态），再跑一次洋红 PS 判定画面。
3. 若 V# 始终找不到合理偏移，再走写监视那条路（文档第四轮 §2）。

---

# 追加：第六轮（2026-11-24 23:39–23:45）—— 表内容是**逐帧变化**的，而且槽位有 32 字节网格

## 1. 新探针 `LEONIDA_LOG_VSHARP`

在 slot 36 处把表 VA 从 `-0x40` 到 `+0x100` 每 4 字节滑一个 16 字节窗口，按 gfx10 V# 打分
（`TYPE==0`、四个 `DST_SEL` ≤ 5、`FORMAT` 1..127、`BASE` 在 arena 内、`BASE+大小` 不越界、`STRIDE` 合理），
只打印得分 ≥ 4 的窗口。前 12 次 slot-36 调用。

## 2. 结果（两个关键新事实）

1. **表内容是逐帧变化的**。同一张合成表（`0xb96dea0`）：
   - 早先某帧 dump 到的是 `{0x90d1e20, 0, 0x90d1e20, 0}`（指针形状）；
   - 这一轮第一帧 dump 到的是 `00000002 00000844 00000bc6 00210440`。
   所以之前把"指针对"当成固定格式的推断**不成立**——那是某一帧的中间状态。
2. **槽位确实是 32 字节网格**，且老表的每个槽前半是"类 V#"、后半是
   `{0, 0, 0xffffffff, 0}`（形如空 UAV counter）：

```
table 1 (0xb94ef00):  +0: 42 0 0c0001 0d | +20: 42 0 0c0001 07 | +40: 42 0 0c0001 06
                      +60: 42 0 0c0001 4a | +80: 42 0 0a0001 97 | ...
```
（每 0x20 字节一个槽，前半 16 字节首 3 个 dword 相同、第 4 个 dword 逐槽变化）
3. 合成表第一帧在 `+0` 解出 `base=0x84400000002`（越界），但**偏移 `+24` 处有一个得分 5 的合理窗口**：
   `{0x48, 0, 4, 0x808}`（base 0x48、num 4、sel 0104）。这提示表 VA 前面可能还有 header，
   或者 slot 内 V# 的真实起点不在我们假设的位置。

## 3. 下一轮（具体）

1. 用 `LEONIDA_LOG_VSHARP` 对**同一次运行里的同一张表**连续采样多帧（把 `nth <= 12` 换成按表 VA 分组、
   记录同一 VA 的多次绑定），看槽内容在帧间怎么变；只有稳定的部分才是描述符。
2. 把 `LEONIDA_LOG_TABLE`（原始字节）和 `LEONIDA_LOG_VSHARP`（打分）一起看合成表：确认第一帧
   `{2, 0x844, 0xbc6, 0x210440}` 这 16 字节是否其实是 **32 字节槽的后半**（即 V# 在 `+16`），
   以及 `+24` 那个"合理窗口"是不是跨槽拼出来的假象。
3. 结论稳定之前**不要**再开 `XS_TABLE_REWRITE`/`XS_TABLE_INDIRECT`（它们会改坏真实描述符）；
   只保留 `XS_TABLE_FIX` 的窗口拷贝做判定。

---

# 追加：第七轮（2026-11-24 23:41–23:50）—— 表是**稳定的**，槽= {A(16B), B(16B)}，A/B 都不是 gfx10 V#

## 1. 新探针 `LEONIDA_LOG_TABLE_STABLE`

同一个表 VA 连续采样，打印"还在变的 word"，再打印"连续 4 次没变"的快照。90 秒跑下来
**"word changed" 计数 = 0**，所有表第一帧之后就不再变化 —— 所以"第一帧看到的是半成品"的猜测**也错了**，
那些字节就是标题写好的最终描述符。

## 2. 稳定快照（关键数据）

合成表 `0xb96dea0`（32 dword）：

```
00000002 00000844 00000bc6 00210440 | 00000041 00000300 00000048 00000000
00000004 00000808 ffffffff 00000000 | 00000041 00000300 00000058 00000052
00000048 00000804 ffffffff 00000000 | 00000041 00000300 00000070 00000004
00000020 00000804 ffffffff 00000000 | 00000041 00000300 00000098 00000022
```

表 `0xb94ef00`（每 32 字节一个槽，槽前半首 3 dword 相同、第 4 个逐槽变）：

```
42 0 0c0001 0d | 0 0 ffffffff 0
42 0 0c0001 07 | 0 0 ffffffff 0
42 0 0c0001 06 | 0 0 ffffffff 0
42 0 0c0001 4a | 0 0 ffffffff 0
```

## 3. 结构性结论

1. **槽 = 32 字节，内含两个 16 字节半区 {A, B}**：
   - A 半区形状：`{lo, hi, ffffffff, 0}`（hi 是 0x808/0x804 之类）或合成表里的 `{2,0x844,0xbc6,0x210440}`；
   - B 半区形状：`{00000041, 00000300, size, flags}`（size/flags 逐槽变化），或 `{0,0,0xffffffff,0}`。
2. **A/B 按 gfx10 V# 或 T# 解码都不成立**（base 越界、TYPE=0、DST_SEL 出现保留值 2/3）。
   结合 xcl 的"槽 32 字节、unified memory"事实，说明这个标题的表**不是**硬件 gfx10 描述符布局，
   或者 V# 落在槽内另一个偏移（`ffffffff` 这个哨兵值出现在槽的第三个 dword，很像"空描述符"标记）。
3. 六轮下来，"猜布局"的所有分支（指针链、半成品、slot 偏移、改写/截断）都已被数据否掉。

## 4. 下一轮必须换方法

**写监视**（文档第四轮 §2）现在是唯一能定论的路：
在 slot 36 给表所在页装 `PAGE_GUARD`，VEH 里抓写表的 `RIP` 与写入值，反汇编写表代码即可知道格式。
本机没有 capstone，但可以用 `dumpbin /DISASM`（MSVC 自带）或按 RVA 用项目 `tmp/dis.py` 的 PE 解析 + 
手写几条指令解码先定位函数。拿到写表代码后，把这段结论写回本节。

---

# 追加：第八轮（2026-11-25 00:05）—— 静态读语料：合成 VS 要的就是标准 gfx10 V#，位置是表 +0x00 / +0x40

写监视之前，先把"着色器到底想要什么"从语料里**静态**读出来 —— 语料目录里就有 `.spvasm`
（`spirv-dis` 输出），不用跑游戏。`(sga_dr_vs_pos_color_tex_c32788bc_Wrapped.spvasm` 的关键路径：

1. 表地址 = `heap_base + ((s9<<32) | (s8 & 0xfffffffc))`（第 425–434 行）。s8 是 user SGPR 基址，
   s9 是 `s_load` 的 64 位地址高半，**必须为 0**，否则地址直接飞出 arena。
2. 在表 **+0x00** 读 4 dword → s4..s7；在表 **+0x40** 读 4 dword → s0..s3（第 438–468 行）。
   即合成 VS 的表布局 = `+0x00` 一个描述符、`+0x40` 一个描述符，和"32 字节槽"一致（槽 0、槽 2）。
   **之前的 `LEONIDA_LOG_VSHARP` 在任意 16 字节窗口上打分，是因为不知道这两个固定偏移；
   现在知道只有这两个位置有意义。**
3. s4..s7 按 **gfx10 V#** 解：`base = (s5 & 0xffff)<<32 | (s4 & ~3)`，`stride = s5[29:16]`，
   `num_records = s6`，随后从 `base + 0x00/0x10/0x20/0x30` 读 4 个 4-dword 行组成矩阵，每一步都按
   `num_records*stride`（stride=0 时用 num_records）做边界检查（第 473–531 行）。
4. s0..s3 同样按 gfx10 V# 解，再 `+0x160` 读顶点（第 669–720 行）。

**结论：格式不再是未知数。** 着色器消费的就是标准 gfx10 16 字节 V#，字段解释与
`docs/d3d12_vulkan.md` §3.3 完全一致。于是第七轮"表里的 A/B 解不出 V#"说明了另一件事：
**在那个 VA 上读到的内存并不是着色器要读的那 16 字节** —— 层写描述符的位置 ≠ 表 VA 指向的位置，
或者两处 VA 只是数值相同的巧合（第一轮"arena 同偏移全零、标题同 VA 有数据"正好对上这个解释）。

## 1. 下一步（从"猜格式"改成"对地址"）

三路对照 dump（一次 40 秒运行）：对 slot 36 收到的合成表 VA，同时打印

- (a) arena 在该偏移的 0x80 字节（着色器实际会读到的）；
- (b) 标题内存同 VA 的 0x80 字节（`copy_committed` 读到的）；
- (c) 该 VA 落在哪个 `DescriptorRange`（CPU/GPU/backing 三个起点），以及层为这张表写过的 T#/V# 字节。

判据：若 (c) 的 backing 与 (a) 不一致 → 写侧/读侧地址模型不一致（架构问题，见本文 §9 第 3 条）；
若 (a) 本该是 V# 却是 0 → `Create*View`/`CopyDescriptors` 根本没写到这张表。

## 2. 写监视降级为备选

只有当三路对照仍然解释不了时，才上 `PAGE_GUARD` + VEH（成本高：要抓 RIP、还要反汇编）。

---

# 追加：第九轮（2026-11-25 00:10–00:20）—— 地址对上了：GPU handle = arena 偏移，但着色器读的那份是 0

## 1. 新探针 `LEONIDA_LOG_TABLE3`（三路对照）

slot 36 对每个（去重后的）表 VA 打四份 80 字节：

- `arena`：`heap_base + va` 在 arena 宿主映射里读到的（**着色器实际读的**）；
- `title`：宿主地址同数值的字节（`copy_committed`）；
- `range-arena` / `range-title`：该 VA 命中 `DescriptorRange` 时，层的 real（arena 内）和标题 backing 上的同一偏移。

另有 `slot k` 行：按 32 字节槽解第 k 槽前 16 字节成 gfx10 V#（base/stride/records/w3），并打印 base 是否
committed、以及 base 处的头 4 个 dword。

复现：

```powershell
powershell -File tmp\run-probe.ps1 -Tag heapva -Seconds 25 `
  -Set 'XS_GPU_HANDLE=1,XS_HEAP_VA=1,LOG_TABLE3=1,DEVICE_FAULT=1,CAPTURE=...\tmp\vbcap2,CAPTURE_LIMIT=3'
```

## 2. 关键发现

1. **不开 `XS_GPU_HANDLE` 时，表 VA 不在任何 `DescriptorRange`（`range 0`）**。此时 `copy_committed` 在
   `0xb94ef00` 读到的东西（第七轮的 `{2,0x844,0xbc6,0x210440}`、`{0x42,0,0xc0001,0xd}`）**是碰巧被提交的
   无关宿主内存**（第 2 节坑 #2 那个 `is_committed` 陷阱），不是标题写的描述符。**前几轮对这堆字节做的
   格式推断全部作废**，包括"槽=32 字节 {A,B}"、"A/B 都不是 V#"。
2. 开 `XS_GPU_HANDLE=1,XS_HEAP_VA=1` 后地址模型确定下来：

   ```
   table3 1: index 0 va 0xb94ef00 handed 0xb94ef00 in_layer 0x4ddba3ef00 backing 1;
             range 1 custom 0xae10000 real 0x4ddaf00000 backing 0x4de00f0000 gpu 1
   ```

   即 **GPU handle 空间 = arena 偏移**（custom 0xae10000 是 heap 的 GPU handle 起点，real 是 arena 里的宿主
   地址，backing 是标题自己的 XMem 映射）。`in_layer = real + (va - custom)` 与
   `0x4ddaf00000 + (0xb94ef00-0xae10000) = 0x4ddba3ef00` 完全一致 → **地址对得上了**。
3. 但同一偏移上：

   - `range-arena`（着色器会读的 arena 副本）：**全 0**；
   - `range-title`（标题 backing）：有数据，形如 `{ptr, 0, ptr, 0}` 的 16 字节对（`ptr` = 0x8ed1e20、
     0xc227210 这类），偶尔夹 `bf800000 3f800000`（-1.0f / 1.0f）。

   而 `LEONIDA_LOG_DESC_WRITE`（另一次 20 s 跑）显示层的 T# 写入都 **in arena 1**，目标在
   `0x4DDAF00260` 这种偏移（arena 偏移 ≈ 0xAE00xxx）。

## 3. 结论：heap 身份错位

层把描述符写进了 heap X（arena 偏移 0xAE00xxx），标题的 `SetGraphicsRootDescriptorTable` 却指到 heap Y
（注册范围从 0xAE10000 起）——两个 heap 的 GPU handle 区不重合，于是着色器在 `heap_base + table_va` 读到
**全 0**，解出 V#=0（base/stride/records 全 0，边界检查通过但读出 0），矩阵 = 0，位置全退化，一个像素都不写。
这与第一轮"arena 同偏移全零"、第三轮"洋红 PS 也不出像素"完全一致。

## 4. 下一轮（具体）

1. 给每个 descriptor heap 打唯一 id，记录 `(CPU start, GPU start, 范围)`：`CreateDescriptorHeap` 时打印一次；
   `Create*View` 时打印 heap id + slot 序号；slot 36 时打印 table VA 落在哪个 heap id。
   对不上就是错位点（最可能是 `GetGPUDescriptorHandleForHeapStart` 的偏移算法或 heap 复用了同一个 handle 区）。
2. 修好后 `range-arena` 应该是标题 backing 里那份数据的副本（或直接就是同一块内存）；再看着色器读到的 V#
   能不能解出一个能读的 base（此时 base 应在 arena 内）。
3. 已弃用：`LEONIDA_XS_TABLE_VBASE`（把 V# base 当标题宿主地址去窗口化）。24 s 跑实测 device lost：
   `fault 0x302e40000 / heap_base 0x202e40000` = `heap_base + 0x100000000`，证明那些 48 位 base 不是标题
   宿主地址。开关保留，默认关。

---

# 追加：第十轮（2026-11-25 00:20–00:30）—— 表不在 descriptor heap 里，它是标题自己的内存

## 1. 新增探针 / 开关（全部 opt-in，默认关）

| 开关 | 作用 |
| --- | --- |
| `LEONIDA_LOG_HEAPID` | 每个 descriptor heap 一行：id、type、槽数、XMem 地址、arena 偏移、GPU handle；前 6 个 heap 还打两侧 slot 0 的字节 |
| `LEONIDA_LOG_SLOTWRITE` | 每次 `Create*View`/`CopyDescriptors` 写哪个 heap 的哪个槽 |
| `LEONIDA_XS_TABLE_SYNC` | bind 表之前把标题 XMem 副本整段 memcpy 进 arena（统一内存的写通） |
| `LEONIDA_XS_CPU_HANDLE_ARENA` | 把 heap 的 CPU handle 直接给 arena 里的真实地址，让标题自己的写入落在 GPU 会读的地方 |
| `LEONIDA_XS_TABLE_VBASE` | 把表 VA 翻译成窗口，并把表里 V#/T# 的 base 逐个翻译成窗口地址（扫不到的项清零） |

## 2. heap 身份对账的结果

```
heap 1: type 0 num 1245184 flags 0x1  title 0x4de00f0000 + 0x2600000 -> arena 0xae10000 gpu_handle 0xae10000
heap 2: type 1 num 128     flags 0x1  ... -> arena 0xd410000 gpu_handle 0xd410000
heap 3+: type 0/2/3 flags 0x0（非 shader-visible），real 不在 arena 内
heap 1 GPU handle getter -> 0xae10000（= real − arena_host_base，两者一致）
```

- **GPU handle 空间 = arena 偏移** 这条成立，getter 与 shim 自算的值完全一致，没有 off-by-base。
- shim 在整轮里只看到 **63 次**写进 heap 1，槽号都 ≤ 103；两侧 slot 0 的字节：标题副本全 0、arena 也全 0
  —— **标题根本不往 heap 里写描述符**（它只调 Create*View，写入走的是 arena）。
- 但标题 bind 的表 VA（0xb94ef00 等）换算成 heap 1 的槽号是 **368504** 起、每帧 +4096。
  heap 1 的 GPU handle 区确实覆盖这个数（槽上限 1245184），可这个槽**从未被任何描述符 API 写过**。
  ⇒ 表 VA 不是 heap 的槽，之前 `DescriptorRange` 命中 heap 1 只是**数值上恰好落在它的 handle 区间里**。

## 3. 表到底是什么

在表 VA（0xb94ef00，标题宿主地址，`is_committed` 为真）读到的 64 KiB，按 gfx10 描述符能正常解码：

```
[rt] vbase +0x2b0: V# 0x8cf584b4 -> 0x313584b4
[rt] vbase +0x2d0: V# 0xb50516a0 -> 0x314516a0
[rt] vbase +0x2f0: V# 0x7ff0000  -> 0x31bf0000
```

即 **V# base 都是"已提交的标题宿主地址"**（0x8cf584b4 / 0xb50516a0 / 0x7ff0000），表本身就在标题内存里，
是标题自己构建的统一内存描述符表。而 `XS_TABLE_SYNC` 证明 XMem 那份 heap 副本在这些槽上是**分配器残留**
（全 0 或 -1.0f/1.0f 之类），也就是说标题的表不在 descriptor heap 里。

同时表里也混着一批 base 形如 `0x80800000004` / `0x84400000002` 的项：**既不是标题宿主地址、也不在 arena 内**，
着色器 `heap_base + base` 读它们就是第十轮那次 device fault（console VA `0x80800000000`）。

## 4. 实验结论

1. `XS_TABLE_VBASE` 全表扫描：第一张表 256 项**全部**成功翻译、0 清零、**不再丢设备**；但跑 25 s 卡在第一次
   重写不动 —— 256 个 4 MiB 窗口 = 1 GB，而 arena 只有 256 MiB，窗口创建失败/极慢。
2. `ARENA_MB=1024` + 只扫表头 0x200 字节、其余清零：稳定、无 fault、抓到 3 帧，**画面仍全黑**；
   因为这几张表的表头项正是那些 `0x808/0x844` 的"不可达 base"，被清零了（只有 1 项翻译成功）。
3. `XS_CPU_HANDLE_ARENA`（把 CPU handle 指进 arena）：第一帧 present 后进程直接退出，需要单独查；
   说明标题确实把 CPU handle 当自己的 XMem 内存在用。

## 5. 下一轮（按优先级）

1. **查 `0x80800000004` 这类地址是谁给的**：它既不在标题宿主内存、也不在 arena，最可能是
   `ID3D12Resource::GetGPUVirtualAddress()` 一类的返回值（标题拿到后写进了自己的描述符表，着色器再
   `heap_base +` 就爆）。把 shim 里所有返回 GPU 地址的 API 打出来，确认是否漏了"减 heap_base"这一步。
2. 若确认是"给标题的地址空间不一致"：统一为 console VA（标题宿主地址），标题写的表就与着色器读的同一个数。
3. 窗口机制要能覆盖多张表：改成固定池 + LRU（bind 时拷进池槽、按需重写描述符），或只翻译"当前绑定着色器
   真正会读的偏移"（偏移量可从语料 SPIR-V 的 `s_load_dwordx4` 常量里取）。

---

# 追加：第十一轮（2026-11-25 00:45–00:50）—— 上游同类问题（BL-4）、VA 区间、以及"描述符值"的写回

## 1. 上游检查（分支 `origin/main` = `82a2aabcc`，本地领先 120、落后 1343）

- 上游新增 `docs/boot_leads.md`，其中 **BL-4 与当前问题完全同题**：
  "a console descriptor word may be a 32-bit GPU address rather than a heap offset … translate a word to the buffer
  device address … the way root-descriptor addresses already go through the arena"，状态是**开放**（还没人做
  census）。所以这不是本地漏了某个已修好的补丁，而是上游也还没解决的同一条线。
- 上游 `028001c82`（VM 选项）里量到：`D3D12ConfigureVirtualMemoryX` 的输出字 `+0x10/+0x18` 是
  **用户态图形 VA 区间**，主机与 GPU 共用同一 64 位空间，所以就是进程的 application 地址区间。本地原来只记日志、
  什么都不填 → 已按上游实现移植（`configure_virtual_memory` 现在写 `[0x10000, 0x7ffffffeffff]`）。
  实测：默认跑法不变（30 s 1800 presents，无 fault），画面仍黑。
- 其它上游提交（Xbox slot 81/86/108、`CreateCommittedResource3`、`slot 73` IID、`rdna2spirv` 接受 vcc 基址等）
  与当前症状关系不直接，未合并；本地与上游在 `d3d12_xs_device.cpp` 上已大幅分叉，直接 merge 不可行。

## 2. 关键机理：着色器契约是"描述符字节 = 哈希键"

`runtime/src/descriptor_table.h`（doc §5.1）：翻译后的着色器把从 `heap_base + 表VA` 读到的
**8 dword 描述符字节**做 FNV-1a，查 set 0 binding 2 的哈希表拿 bindless 索引。所以表里的字节**必须与层在
Create*View 时写进 heap 的字节逐字节相同**，否则哈希查不到 → 全走 fallback 0。

而 xcl 记录的 console 语义是"descriptor 是值，标题自己在 CPU-only heap 里创建、再用 memcpy 拷进 shader-visible
heap"。本项目的实现里，层把描述符写进 **arena**（翻译后的 handle），标题从 **XMem CPU handle** 拷 —— 那里是
分配器残留，于是标题把垃圾拷进自己的表，着色器读到垃圾 → 黑屏。这与"arena 同偏移全 0 / 标题副本是残留"的实测
完全一致。

## 3. 新开关 `LEONIDA_XS_DESC_SYNC`（描述符写回）

`Create*View`（CBV/SRV/UAV/RTV/DSV/Sampler）与 `CopyDescriptorsSimple` 之后，把 arena 里写好的 32 字节槽
拷回标题传入的 CPU handle 内存，使两边一致；`CopyDescriptorsSimple` 之前还会把源槽从标题侧拷到 arena。
配合 `XS_TABLE_VA`（把表 VA 翻译成窗口，让着色器读标题写的表）实测：

```
（未开写回）fault console VA 0x80800000000
（开写回）  fault console VA 0x1500ba4e000
```

故障点变了 ⇒ 着色器读到的描述符**确实变了**（写回生效），剩下的是描述符里 base 的地址空间问题（见下）。

## 4. 下一轮

1. 剩下的 fault 来自 base 形如 `0x1500ba4ef00` 的 V#：这是**标题自己的 console VA**（标题内存里的资源），
   而着色器做 `heap_base + base`。根因是层的描述符 encode 直接把资源的标题 VA 写进 V#，没有先映射成
   arena 可达地址 —— **就是上游 BL-4**。修法：在写 V#/T#（或写回前）把标题内存资源的 base 换成
   `guest_translate`/`upload_translate` 之后的 arena 地址（root descriptor 已经这么做了，描述符没有）。
2. 修好后再看哈希键是否匹配：若仍全走 fallback，说明层的 key 与标题表里的字节还有差异（例如 base 已被改写
   导致 key 不同），届时需要让层用"标题表里的原始字节"作为 key 插表。






