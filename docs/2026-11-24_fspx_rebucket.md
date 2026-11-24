# FSPX 重新分桶：236 条 miss 归零，以及黑帧的下一步

接 `2026-11-24_upstream_merge_and_white_window.md`。用户要求用今天 dump 的 payload 重新跑
unpack→disasm→translate，拿到新的失败分布再决定攻哪类。做完了，结论比预期好：**236 条 miss 全部消失**。

## 1. 方法（8 路并行）

```powershell
# 234 个 payload 分 8 组，各自 unpack + 反汇编
tmp\rebucket_unpack.ps1        # 组内用 llvm-mc，--out tmp\rb-unpack-W
# 每组一个翻译进程
tmp\rebucket_translate.ps1     # rdna2spirv.py tmp\rb-unpack-W\disasm --out tmp\rb-spirv-W
```

`fspx_unpack.py` 没有 `--jobs`，所以并行放在进程级：8 份输入、8 个 out 目录，翻译再按目录分 8 个进程。

## 2. 第一次分桶（llvm-mc 反汇编）

234 个 payload，**50 翻出、184 失败**：

| 原因 | 数量 |
| --- | --- |
| fall-through into undecodable words | 162 |
| undecodable words (cannot re-anchor) | 8 |
| s_flbit_i32_b64 of a lane mask | 4 |
| v_fma_mix_f32（反汇编丢字段） | 5 |
| v_cmp destination | 2 |
| 其余各 1 | 3 |

昨晚的 `CS thread-group-size SGPR`（31）和 `ds_append`（10）**已经消失**——是今天摘的那五条翻译器提交
（`0fc8a15c5`、`56200058e`、`6ecd922d9`、`1b15cd231`、`66607102f`）的效果，这两类可以划掉了。

## 3. 最大一类的根因：反汇编器，不是翻译器

`fall-through into undecodable words` 在 `reachable_code()` 里触发：控制流顺着 fall-through 走进 llvm-mc
跳过的那几个字（`.s` 头写着 `N invalid words`）。实测：

| 检查 | 结果 |
| --- | --- |
| 234 个 listing 里有 invalid words 的 | **171** |
| 其中 invalid words 的总数 | 5,455（108 个程序，单条最多 142 处） |
| 同一条流用 `tools/rdna2_decode.py` 解 | **0 个坏字**、3,442 条指令、正常收在 `s_endpgm` |
| 把 `0x08 0x07 0x9c 0xf0` 直接喂 llvm-mc（gfx1030/1010/1036/1100） | 四个 CPU 全部 `invalid instruction encoding` |

也就是说：**这些“坏字”是 llvm-mc 的视角，不是流本身的问题**。我们自己有解码器，而且它能完整解出来。

### 修法一：`fspx_unpack.py --backend python`

新增 `--backend {llvm-mc,python}`，把 `shader_disasm.disasm(..., backend=...)` 透传下去。用 python 后端重跑：

- 有 invalid words 的 listing：171 → **6**；
- 翻译：50 → **104 / 234**。

### 修法二：`vcc_hi` 之类的掩码目标（116 条）

第二次分桶的最大项变成 `v_cmp destination`（96）和 `mask destination`（20）。原因：自研解码器把
`v_cmp_*` 的 b64 掩码目标写成 `s26` / `vcc_hi` 这类形式，而 `mask_dst()` 只认
`exec/exec_lo/null/sgpr/vcc/vcc_lo`。上游 `47494e725`
（"support null/exec/wave32-vcc_hi compare SDSTs and wave32 vcc_hi masks"）正是补这个，`git apply --check` 干净，
已套到 `tools/rdna2spirv.py` + `tools/lane_masks.py`。

第三次分桶：**220 / 234 翻出（94%）**，剩余 14 条是零碎的：

| 原因 | 数量 |
| --- | --- |
| s_flbit_i32_b64 of a lane mask | 4 |
| v_max_f32_dpp（编码字段对不上） | 2 |
| v_nop_e64（同上） | 3 |
| s_nor_b64 source / s_bitreplicate / s_subvector_loop_end / s_load operands slist vcc | 各 1 |

## 4. 并库与实机结果

```text
corpus 27145 -> 27365 entries: added 220, already present 0, conflicting 0
```

重跑游戏（同一套环境变量）：

```text
no translated compute: 0
slot 67 fail: 0
```

**236 → 0**：标题的 FSPX compute PSO 全部建成，`slot 67` 一次失败都没有。这是今晚之后第一个"红字清零"。

## 5. 但画面还是黑

`LEONIDA_CAPTURE` 抓帧（`tmp\cap4\index.txt`）：第 1、25,001、50,001 帧全部 `mean rgb 0.00 0.00 0.00`，
大约两分钟里没有一帧有内容。同时：

- `PresentX` 稳定 `S_OK`，约 450 次/秒（仍然没有帧间隔，说明还没进入受 vsync 节流的正常出帧）；
- 标题**确实在发 Draw**：`list slot 12`（`DrawInstanced`）、`list slot 48`（`IASetVertexBuffers`）都已首次调用；
- 没有任何 `no translated vs/ps`、没有 PSO 建立失败、没有 `E_INVALIDARG`；
- GPU 占用只有 5–7%，基本空闲。

所以现在的形态是"**在发绘制调用，但送上去的帧是黑的**"，和之前的"没渲染"是两件事。

## 6. 下一步的候选（按可疑度）

1. **呈现纹理是不是被当渲染目标写过**。`first present plane` 拿到的是 3840x2160、`ALLOW_DISPLAY` 的资源；若标题只往离屏目标画、再用 compute/拷贝合成，那么合成这一段要么没跑、要么写错目标。验证方式：在 `CreateRenderTargetView` 处记下资源指针，和首次 present 的 `00000001553E0C60` 对比。
2. **`XGCreateTextureComputer` / `XGSuggestSwizzleMode` 仍是 `E_NOTIMPL`**（`xg_xs`）：标题用它做控制台纹理布局转换，拒掉之后纹理内容可能一直是黑的。这条以前"不致命"，但现在 PSO 都通了，它可能是下一个真正的拦路虎。
3. **帧率异常**（450 次/秒、无 vsync）说明标题的帧循环还没拿到帧间隔，可能仍在某个初始化循环里；可以对照 `SetFrameIntervalX`/`ScheduleFrameEventX` 的调用序列确认。

## 7. 仓库状态

本轮新增两个工具改动（未推送）：`tools/fspx_unpack.py`（`--backend`）、`tools/rdna2spirv.py` + `tools/lane_masks.py`
（上游 `47494e725` 的掩码目标支持）。语料在仓库外，已从 27,145 涨到 27,365，索引备份在
`tmp/corpus-index-before-rebucket.json`。
