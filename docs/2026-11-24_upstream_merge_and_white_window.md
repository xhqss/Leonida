# 上游切换、合并价值判断，以及白屏的真相

2026-11-24 14:00 起这一段。前一份记录是 `2026-11-24_overnight_bringup.md`，本文修正它最后一节的结论：
**白屏不是"在渲染但内容白"，而是每一帧 PresentX 都被拒绝**。

## 1. 上游切换

用户指定的新上游：<https://github.com/ubiquitarianism/leonida>（仓库自述 "A continuation of Leonida, the Xbox > PC
port of GTA VI"，2026-11-23T23:54Z 创建，2026-11-24T06:01Z 推送）。

```powershell
git remote set-url origin https://github.com/ubiquitarianism/leonida
git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 fetch origin main
```

直连 `github.com:443` 三次都失败（`curl 28` 两次、`Connection was reset` 一次，各约 20 s）；用户指示改用本机代理，
之后 `git fetch` 3 s 完成。`origin/main` = `8f481cf16`。

| 项 | 值 |
| --- | --- |
| merge base | `952be9b04` |
| 本分支独有提交 | 67 |
| 上游独有提交 | 1043 |
| 干跑合并冲突（`git merge-tree --write-tree`） | **20 个文件** |

冲突文件包含我们赖以启动的全部要害：`d3d12_xs_device.cpp/.h`、`d3d12_vulkan_device.cpp`、`d3d12_xs.cpp`、
`dstorage.cpp`（add/add）、`pe_loader.cpp/.h`、`main.cpp`、`shader_library.h`。

## 2. 合并价值判断：不整体合并，按需摘取

**结论：1043 个提交的整段合并不值得现在做，价值低于风险。**

理由（都可复核）：

1. **上游是另一条实现线**。它有 `runtime/shims/acphal.cpp`、`xapu_chad.cpp`、自己的 `dstorage.cpp`，而我们是
   `runtime/shims/audio.cpp` + 自己的 DStorage zlib。两边把同一批控制台 shim 各写了一遍，整体合并等于让两套实现
   在同一个 `shims.h` / `main.cpp` 里汇合，冲突是语义级的，不是文本级的。
2. **上游没有我们依赖的 PresentX 控制台桥**。`present_state_for`、`LeonidaPresentX` 在 `origin/main` 里不存在
   （`git grep` 无命中）；上游提供了 `find_title_window`（`ec41c2201`）但没有任何调用者，后来那个头文件也不在
   `origin/main` 的树里。也就是说白屏这条路只能我们自己走，合并换不来它。
3. **风险不对称**。当前分支是唯一已知"能活、能进渲染循环"的状态；整段合并要在 20 个文件上重新验证一遍启动链，
   而收益集中在少数几个孤立提交上。
4. **上游确实有好东西，但都可以单独摘**（下一节已实测）。

## 3. 已摘入的提交（全部本地，未推送）

| 提交 | 内容 | 结果 |
| --- | --- | --- |
| `71f66608b` | 本地：PresentX 使用标题自己的窗口 | 窗口不再是我们造的隐藏窗口 |
| `7a0879614` | `LEONIDA_CAPTURE` 把呈现的帧写成 PPM | 抓帧可用 |
| `faa0af266` | 复用 capture staging 前先完成上一帧 | 配合上一条 |
| `97b155dda` | 设备 `WaitFrameEventX` 与 pacer 共用一个 token 源 | **PresentX 由 E_INVALIDARG 变成 S_OK** |
| `6d6065ba3` | 五条翻译器修复（`ds_append`、TG size、EXEC 等，只取 `tools/`） | 两个样本仍失败，见 §5 |

`afb14d56c` 的补丁没有自带调用点（上游没有 PresentX），已在 `present_state_for` 里接上
`device_frame_tokens(device)`，这一行是白屏修复的关键。

## 4. 白屏的真相（推翻旧结论）

旧记录 §9.7 说"四分钟 71,518 次呈现，所以它在渲染，只是画面白"。实际给 `PresentX` 加上返回值日志后：

```text
[rt] d3d12_xs: PresentX failed with 0x80070057 (1 failure(s) so far)
[rt] d3d12_xs: PresentX failed with 0x80070057 (600 failure(s) so far)
...
```

`0x80070057` = `E_INVALIDARG`，**每一次都失败**。旧计数器数的是调用次数，不是成功呈现的帧数。

两个独立原因，缺一不可：

1. **呈现进了一个隐藏窗口**。`present_state_for` 用 `CreateWindowExW` 自建 `LeonidaPresentX` 窗口，没有
   `WS_VISIBLE`，所以是隐藏的（实测 `IsWindowVisible=false`，`2304x1235`）；标题自己建的 `sgaWindowClass` /
   `Americas` 窗口（可见，`2037x937`）什么也没收到。窗口白，是因为它从来没被画过。
2. **token 不是同一个源**。设备槽位 80 `WaitFrameEventX` 用自己的计数器发 token，而 `XboxPresentation` 的 pacer
   只承认自己发出的 token，于是每一帧都以"未知 token"被拒（`E_INVALIDARG`）。

修复后：

```text
[rt] d3d12_xs: PresentX presents into the title's window 00000000004504A2 (client 3076x1709, visible 1)
[rt] d3d12_xs: PresentX returned S_OK
[rt] present: capture frame_00001.ppm (3076x1709, mean rgb 0.00 0.00 0.00)
```

窗口从白变黑，抓到的帧也是纯黑——**呈现链路通了，标题送来的帧本身是黑的**。

## 5. 现在的停点：黑帧

- 12 帧抓取跨越 24,600 次呈现，`mean rgb` 全部 `0.00 0.00 0.00`；不是"加载中的黑屏"，是稳定黑。
- 同一次启动仍有 **236 条** `no translated compute shader for FSPX`，`slot 67` 全部 `E_INVALIDARG`。
- 最大类 `fall-through into undecodable words`（236 里的 160）**不是翻译器的语义问题**：对应 `.s` 的头部写着
  `13 invalid words`，也就是反汇编这一层就放弃了那几组编码。用 `origin/main` 上最新版翻译器（含 64 个提交、
  额外的 `shader_ir` / `cfg_structure` 等模块）跑同一条样本，报的是同一个错误 → **瓶颈在反汇编/解码表**。
- 因此下一步不是继续搬翻译器语义提交，而是：把那些 invalid word 的编码抓出来（`.s` 里能定位到偏移），
  补 `llvm-mc` 或 `tools/` 自己的解码表；补完再按 §15.1 的链重翻这批 payload。

## 6. 复现命令（当前二进制）

```powershell
cd E:\temp\wait_work\Leonida
$env:LEONIDA_GUEST_TLS_SLOT0='1'
$env:LEONIDA_SHADERS='E:\temp\wait_work\leonida-work\shaders\spirv-561b65a'
$env:LEONIDA_D3D12='vulkan'; $env:VK_LOADER_LAYERS_DISABLE='~implicit~'
$env:LEONIDA_SKIP_ASSERTS='1'
# 抓帧（可选）：每 30 帧一张 PPM，最多 4 张
$env:LEONIDA_CAPTURE='E:\temp\wait_work\Leonida\tmp\capture'; $env:LEONIDA_CAPTURE_EVERY='30'
$env:LEONIDA_CAPTURE_LIMIT='4'
.\runtime\build-561b65a\gta6rt.exe --run `
    'E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK\game_scarlett_master.exe' `
    --game-dir 'E:\temp\wait_work\GTA6.Xbox.v1.0.2.1-CYBERLEEK'
```

判断标准：日志里 `PresentX returned S_OK` 出现、`PresentX failed` 不出现；`tmp\capture\index.txt` 的
`mean rgb` 是否仍为 0。

## 7. 回归检查

- 重编 `tmp\build-gta6rt.cmd all` = BUILD-OK（本轮多次）。
- 未跑完整 `ctest`：本轮的改动集中在 PresentX 呈现路径与 `tools/rdna2spirv.py`，串行 ctest 留待下一次。
- 语料仍为 27,145（未新增条目）；`pipeline_cache.bin` 未重建。
- 工作树只剩未跟踪的 `.idea/`、`tmp/`；**全部提交都在本地，未推送**。
