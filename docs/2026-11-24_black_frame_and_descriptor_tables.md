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
