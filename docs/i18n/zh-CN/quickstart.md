# Leonida — 快速上手（`zh-CN`）

> [!NOTE]
> 英文文档（[`README.md`](../../../README.md)、[`CONTRIBUTING.md`](../../../CONTRIBUTING.md) 和
> [`docs/README.md`](../../README.md)）是**权威版本**。本文只汇总稳定的安装与构建说明（与 commit `003e7812` 同步）。
> 项目每日进展请查看 [`README.md`](../../../README.md)。

## Leonida 是什么

Leonida 是一个把 Xbox Series 版 Grand Theft Auto VI **静态重编译**到 Windows 的项目：在**你自己的电脑上、
用你自己的游戏拷贝**构建。主机的 RDNA2 着色器翻译为 SPIR-V（Vulkan），资源直接从你的包读取，
主机的系统软件用 shim 重新实现。

> [!IMPORTANT]
> 本仓库**不包含**游戏、密钥或可执行文件，也不说明如何获取。这里没有任何解密或绕过保护的内容。构建产生的一切
> （翻译后的着色器、导出的资源、工作目录和存档）都来自你自己的拷贝，**只能留在本机**——不要在 issue 或
> pull request 中附带或分享这些文件（见 [`CONTRIBUTING.md`](../../../CONTRIBUTING.md)）。

项目处于早期：**目前还不能启动游戏**。

## 前置条件

- **你自己的拷贝**：Xbox Series 包，版本 `1.0.2.1`（目录里有 `MicrosoftGame.Config` 和明文可读的 `.rpf` 文件）。
- **Windows 10/11 x64**，约 **350 GB** 空闲空间给中间文件（可用 `--work` 放到另一块盘）。
- [Python 3.12+](https://www.python.org/downloads/)（勾选 “Add python.exe to PATH”）。
- [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)。
- [Vulkan SDK](https://vulkan.lunarg.com/)（`spirv-as`、`spirv-val`、`vulkaninfo`）。
- [LLVM 21+](https://github.com/llvm/llvm-project/releases)，需要 **AMDGPU target**（`llvm-mc`）：
  - Windows 官方 LLVM 发行版**不含** AMDGPU target。
  - Windows 上运行 `scripts\build_llvm_mc.ps1` 构建原生 `llvm-mc.exe`（需要 `git` 和 Visual Studio 的 C++ 工具）
    并设置 `LLVM_MC`；**或**在 WSL 里安装 `llvm-21` 并设置
    `LLVM_MC="wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc"`（见 [`docs/design.md`](../../design.md#disassembling-shaders)）。
  - Linux 上 `PATH` 里有 `llvm-mc` 即可。
- **Visual Studio 2022**，勾选“使用 C++ 的桌面开发”和 **Windows SDK `10.0.26100` 或更新**
  （`runtime` 阶段需要，它使用 root signature 1.2）。
- [ffmpeg](https://ffmpeg.org/download.html) 在 `PATH` 中（可选：解码 XMA2 流；使用 `--wav` 时必需）。

## 如何构建

### 简单方式（Windows）

1. 安装上面的前置条件。
2. **带子模块**克隆仓库：

   ```powershell
   git clone --recursive https://github.com/ItsNotRexed/Leonida
   ```

3. 双击 **`Build.bat`**。

脚本会自动查找你的拷贝（Downloads、Desktop、Documents（也包括 OneDrive 等 Windows 实际存放它的位置）、Games、
用户目录以及每个盘的根目录往下两层），检查前置条件和可用空间，结果写到 `Documents\Leonida`。构建中断后重新运行即可从断点继续。

### 命令行（`make_game.py`）

```powershell
python -I make_game.py --find                             # 列出本机找到的拷贝
python -I make_game.py --pick                             # 在文件夹对话框里选择游戏目录
python -I make_game.py --check                            # 校验游戏文件，不构建
python -I make_game.py --check --full-check               # 另外比对每个文件的 SHA-256（较慢）
python -I make_game.py --game "D:\path\to\package" --out "D:\Games\Leonida" --work "E:\leonida-work"
python -I make_game.py --stages shaders,assets            # 只跑指定阶段
python -I make_game.py --game "D:\path\to\package" --wav  # 额外把 Ogg Opus 解码为 PCM WAV（很大）
python -I make_game.py --wave64-emulate auto              # auto（默认）、on 或 off
```

阶段顺序：`check` → `shaders` → `assets` → `runtime` → `pipelines`。

- **`--wave64-emulate`**：决定是否把 wave64 compute/mesh 着色器翻译成在任意子组宽度下都精确的版本
  （见 [`docs/design.md`](../../design.md) 中 Wave64 emulation 一行）。NVIDIA 和 Intel GPU 是 32 lane 子组，
  需要它。默认 `auto` 会询问 `vulkaninfo`（属于 Vulkan SDK），除非所有 GPU 都报告 64 宽子组、或无法判断，
  都会启用；运行时实际使用的 GPU 无法以 64 宽运行 compute/mesh 时也会启用。有一个例外会让它保持关闭：当运行时的
  GPU 是 RDNA2 独显或核显（RX 6000、Ryzen 6000/7000/9000 的核显）、使用 AMD Windows 驱动并原生运行 wave64 时
  ——该驱动的着色器编译器在模拟 LDS 代码上会崩溃（#857）；否则 Intel 核显搭配 RX 6000 卡时也会被启用。
  该模式会保存在翻译后的着色器旁边，切换它会重新翻译。使用 `auto` 时，之前用过模拟的工作目录在后续翻译和重试中
  会继续使用模拟，即使当前 GPU 支持原生 wave64；要用原生方式翻译，使用 `--wave64-emulate off`。
- **四个 fallback 标志**：它们同样转发给翻译器、保存在翻译后的着色器旁边，切换会重新翻译：
  `--no-float-controls`（去掉 `SignedZeroInfNanPreserve`，用于设备在缺少
  `shaderSignedZeroInfNanPreserveFloat32` 时会跳过着色器；需要 PR #664 的翻译器标志）、
  `--bary-fallback`（从插值输入读取原始重心坐标，用于没有 `VK_KHR_fragment_shader_barycentric` 的设备）、
  `--texel-offset-fallback`（把 `image_sample*_o` 的偏移折叠进坐标，用于没有 `VK_KHR_maintenance8` 的设备）和
  `--mesh-compute`（把 mesh 着色器当作 compute，用于没有 `VK_EXT_mesh_shader` 的设备）。每个标志的说明见
  [`docs/design.md`](../../design.md)。
- **`--work`**：建议指向至少有 350 GB 空闲的磁盘；`shaders` 阶段最慢，默认使用一半 CPU 线程（`--jobs` 可调）。
- **Linux 与 Wine/Proton**：`make_game.py` 也能在 Linux 上运行。`runtime` 阶段会在 Linux 上用 clang-cl 和
  lld-link（LLVM 19+，但 22.1.7 和 22.1.8 有已知问题，见 [docs/testing.md](../../testing.md#known-problems)；
  命令名用不带版本后缀的，或五个 LLVM 工具都用同一版本后缀，例如 `clang-cl-21`）把 loader 交叉编译到
  `runtime/build/windows`，依赖 `XWIN_DIR` 或
  `~/.local/share/xwin-sdk` 里的 MSVC CRT 和 Windows SDK
  （[docs/testing.md](../../testing.md#the-windows-runtime-on-linux-under-wine-or-proton) 有一次性 `xwin` 命令）；
  缺少这些时该阶段不参与默认 stages，显式请求会先列出缺什么。
  也可以使用别处构建好的 Windows 版本，例如 Runtime workflow 的 `runtime-windows` artifact：
  `python3 -I make_game.py --runtime-build ~/Downloads/runtime-windows`。
  两种方式都会把 `gta6rt.exe` 和 `gta6rt_tls_host.dll` 复制到 `--out`，并在旁边生成 `gta6rt.sh`，
  用 `wine`（或 `WINE` 指定的 Proton）启动。

## 常见问题

- **用 GitHub 的 “Download ZIP” 下载**：ZIP 不含 `third_party/rpf9-explorer` 子模块。请用
  `git clone --recursive`，或在仓库目录里执行 `git submodule update --init --recursive`。
- **`llvm-mc` 拒绝 `gfx1013` / AMDGPU**：多半是用了 Windows 官方 LLVM 安装包。改用
  `scripts\build_llvm_mc.ps1`，或把 `LLVM_MC` 指向 WSL 里的 `llvm-mc`（`llvm-21`）。
- **构建 runtime 时报 `D3D12_ROOT_SIGNATURE_DESC2` 错误**：把 Windows SDK 升级到 `10.0.26100` 或更新
  （可在 Visual Studio Installer 或 `winget install Microsoft.WindowsSDK.10.0.26100` 中安装），
  删除构建目录后重新配置。
- **`C:` 盘空间不足**：用 `--work "D:\leonida-work"` 把中间文件放到其他分区。

## 参与贡献与运行 GPU 测试

- GPU 测试指南（不需要游戏文件）：[`docs/testing.md`](../../testing.md)。
- 贡献规则、零游戏数据政策与任务协调：[`CONTRIBUTING.md`](../../../CONTRIBUTING.md)。
