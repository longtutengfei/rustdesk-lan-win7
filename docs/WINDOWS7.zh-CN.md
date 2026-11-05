# Windows 7 SP1 x64 兼容构建

此改动为 `tyaro/rustdesk-lan` 增加独立的 Windows 7 构建配置。目标是让
Windows 7 SP1 **64 位**及更新的 Windows 系统使用同一套局域网客户端。
保留项目原有的 IP 直连、局域网发现和远程桌面逻辑。

**当前交付状态：Windows 构建成功，候选 ZIP 已上传；尚未进行 Win7、Win10、Win11 实机测试。**
[Actions 构建 #5](https://github.com/longtutengfei/rustdesk-lan-win7/actions/runs/42048983180)
已完成 Windows 编译、链接和 PE 预检，产物仍是待实机验收的 Win7 兼容候选包。
不能把源码检查、依赖解析或 PE 导入检查通过当作已经验证支持 Win7。
本次不包含 XP、Server 2003、Win7 32 位的适配。

基于上游提交：`172e8dcbd9bd0a270677c8c7f03e404c33513a91`。
`libs/hbb_common` 子模块提交：`e5d9890716f4ee6d1360d9c2bbd593a6ca0ab438`。

## 为什么要单独构建

主工程使用的 Flutter 版本与当前 Rust stable 不能仅靠更改 Windows 版本号
就获得 Win7 兼容性。Rust 官方从 1.78 起提高了常规 Windows target 的最低
系统要求；RustDesk 官方也记录了 Flutter 3.24.5 在 Win7 上需要修改引擎。

本配置锁定以下组件，并使用独立的 UI 副本和输出目录：

| 组件 | 固定值或处理方式 |
| --- | --- |
| 目标系统 | Windows 7 SP1 x64 及更新 Windows；每种系统仍需实测 |
| Rust | 1.75.0，`x86_64-pc-windows-msvc` |
| Flutter / Dart | Flutter 3.24.5 / Dart 3.5.4 |
| Flutter 引擎 | RustDesk 提供的 Win7 兼容引擎，下载后校验固定 SHA-256 |
| Flutter 依赖 | `scripts/windows7/pubspec.lock`，构建时强制锁定 |
| Rust 依赖 | 原工程 `Cargo.lock`，构建时使用 `--locked` |
| 原生音视频库 | 固定 vcpkg baseline，独立 `x64-windows-static` triplet |
| 编解码 | 软件编解码，不启用 `hwcodec` / `vram` |
| Rust 与界面交互 | FRB codegen 1.80.1，由兼容构建生成桥接代码 |

不要把这个 `flutter_windows.dll` 单独覆盖进 Flutter 3.38 的现成安装包。
Flutter SDK、引擎与生成的 `app.so` 必须来自匹配的一次构建。

## 构建机器与使用机器

**构建机器**使用现代 Windows x64，安装开发工具并联网下载依赖；推荐使用
仓库内的手动 GitHub Actions 工作流。无需在 Win7 电脑上安装开发工具。

**使用机器**拿到构建产物后，把完整运行目录复制到局域网电脑，运行
`rustdesk-lan.exe`。IP 直连不需要部署 ID/中继服务器，也不要求互联网连接。
不要只复制 EXE：Flutter 引擎、`librustdesk.dll` 与 `data` 目录都要保留。

局域网启动使用 IP 直连和 LAN 发现，已移除启动时的中心服务器注册、中心
HTTP 心跳和自动更新线程。
“不依赖互联网”不等同于已对程序每个可选功能做过零外联流量审计。

### 拿到候选程序后的使用步骤

1. 在两台电脑上解压完整程序目录，运行 `rustdesk-lan.exe`。两台电脑需要
   在可互通的局域网中；使用机器不需要安装 Rust、Flutter 或编译工具。
2. 被控电脑保持程序运行。如果 Windows 出现防火墙提示，允许该程序访问
   你实际使用的局域网。
3. 在控制电脑输入被控电脑的 IP，例如 `192.168.1.20`，发起连接。默认直连
   端口为 TCP `21118`；修改过端口时输入对应的 `IP:端口`。
4. 按被控端的密码或确认设置完成认证，然后检查画面、鼠标与键盘。

新配置默认启用 IP 直连；已有配置中明确关闭的选项会保留。若以前手动关闭过
IP 直连，需要在设置中重新开启。程序不会因为是局域网版而跳过连接认证。
界面就绪状态以实际 IP 监听结果为准，无需等待中心服务器上线。

## 推荐方式：GitHub Actions

将这些改动放入你有写权限的 RustDesk-LAN 仓库后，使用仓库内新增的
`.github/workflows/windows7-build.yml`。这是手动工作流，不自动发布 Release。

1. 在 GitHub 仓库打开 **Actions**，选择
   **Build Windows 7 LAN compatibility candidate**。
2. 选择包含本改动的分支，点击 **Run workflow**。
3. 等待依赖安装、桥生成、Rust 编译、Flutter 编译和 PE 预检完成。
4. 下载该次运行的 artifact。候选程序 ZIP、PE 检查结果、构建来源记录
   都来自这一次运行；如果编译或检查失败，不会生成可分发的候选程序 ZIP。
5. 把完整候选程序目录带到离线电脑上，完成下文的实机测试。

本仓库的构建记录可在
[Windows 7 Actions 工作流](https://github.com/longtutengfei/rustdesk-lan-win7/actions/workflows/windows7-build.yml)
查看。以成功运行生成的候选包及其中的来源记录为准。

工作流在编译前下载并校验固定的兼容引擎，随后缓存已验证的归档。缓存命中
仍会重新校验 SHA-256 和归档内容，最终打包时再检查引擎未被 Flutter 替换。

## 本地 Windows 构建

详细依赖安装步骤以 `.github/workflows/windows7-build.yml` 为准，可在一台
Windows 10/11 x64 或 Windows Server 构建机上重复执行。

需要 MSVC C++ 工具链和 Windows SDK、Git、Python、Flutter 3.24.5、
Rust 1.75.0（含 `rustfmt`）、LLVM/libclang、NASM、FRB codegen 1.80.1 和
`pefile==2024.8.26`。请从 x64 MSVC 开发者命令行运行。

源码需要包含 `libs/hbb_common` 子模块。基于 Git checkout 构建时先运行：

```powershell
git submodule update --init --recursive
rustup toolchain install 1.75.0 --profile minimal --component rustfmt
cargo +1.75.0 install flutter_rust_bridge_codegen --version 1.80.1 --features uuid --locked
python -m pip install pefile==2024.8.26
```

将独立的 vcpkg checkout 固定到以下提交并 bootstrap：

```text
120deac3062162151622ca4860575a33844ba10b
```

从仓库根目录设置实际路径，并安装本配置的原生依赖：

```powershell
$env:VCPKG_ROOT = 'C:\build\vcpkg-win7'
$env:VCPKG_INSTALLED_ROOT = 'C:\build\vcpkg-win7-installed'
$env:VCPKG_DEFAULT_HOST_TRIPLET = 'x64-windows-static'
$env:LLVM_PATH = 'C:\Program Files\LLVM'
& "$env:VCPKG_ROOT\vcpkg.exe" install `
  --triplet x64-windows-static `
  --overlay-triplets=scripts/windows7/triplets `
  --overlay-ports=res/vcpkg `
  --x-manifest-root=scripts/windows7 `
  "--x-install-root=$env:VCPKG_INSTALLED_ROOT"
```

然后运行专用入口，传入 Windows 版 Flutter 3.24.5 SDK 的真实路径：

```powershell
python scripts/windows7/build.py --flutter-sdk C:\build\flutter-3.24.5
```

如果 LLVM 安装在其他位置，可加 `--llvm-path C:\build\LLVM`；这里传入
安装根目录，入口会查找其 `bin/libclang.dll`，并让 Rust bindgen 与 Dart
ffigen 使用同一份库。

入口检查 SDK/引擎版本、工具链、锁文件以及最终打包的引擎。它使用
`target/windows7/` 中的隔离副本，不直接降级主工程的 `flutter/pubspec.yaml`。
若之前生成过副本而源文件发生变化，入口会要求使用新的工作目录，例如：

```powershell
python scripts/windows7/build.py `
  --flutter-sdk C:\build\flutter-3.24.5 `
  --workspace target/windows7/build-2 `
  --output target/windows7/dist-2
```

构建成功后的默认输出目录是 `target/windows7/dist/`。文件名含
`windows7-x64-candidate`，表示仍须做目标系统的实际运行验证。

## 兼容配置的功能取舍

- 保留局域网远程桌面相关代码、文件传输与剪贴板功能；这些功能在 Win7
  上的实际可用性仍列入验收，不以源码保留作为通过证明。
- 使用软件编解码。Win7 的桌面捕获保留上游已有的 GDI 回退路径。
- 不打包可选虚拟显示器和虚拟打印机驱动，也不打包仅适用于较新 Windows
  的虚拟显示 DLL。
- Win7 构建关闭 WinRT toast 通知依赖，避免启动时依赖 Win7 不具备的系统
  DLL；普通构建的默认通知行为保持原样。普通安装结果使用系统消息框，
  静默安装只输出日志/文字，不等待用户关窗。
- 远程终端依赖较新 Windows 的 ConPTY，不作为 Win7 支持功能。
  Windows 10/11 上也要独立测试，不能用远程桌面成功推断终端成功。

Runner 清单增加旧版 DPI 声明；动态查询新 DPI API 的路径在 API 不存在时
也正确释放 `user32.dll` 的引用。这里的修改不会证明或修复 SubnetDesk 的
黑屏问题；两个项目的实际问题需要分别验证。

Win7 的 Rust 构建显式使用 `--no-default-features --features use_dasp,flutter,windows7`，
并对目标依赖统一设置 `-Ctarget-feature=+crt-static --cfg windows_slim_errors`。
同时启用 `windows7` 与 `windows-toast` 会编译失败；遗漏必需 cfg 也会停止
构建，以防误生成仍携带 WinRT 导入的“兼容包”。

## PE 检查能证明什么

`scripts/windows7/audit_pe.py` 扫描整个候选运行目录的 EXE/DLL，而不执行程序。
它检查机器类型、PE 最低系统/子系统版本、导入 DLL、普通与延迟导入、
导入符号及转发依赖。

没有 Win7 系统参考目录时，检查结果只能是 `preflight-only`。它能发现
已知不兼容接口，但不能保证所有未列出的 Windows 接口均存在。

如果你有实际 Windows 7 SP1 x64 的 `System32` 副本，可以在构建时提供：

```powershell
python scripts/windows7/build.py `
  --flutter-sdk C:\build\flutter-3.24.5 `
  --system-dir D:\reference\win7-sp1-x64\System32
```

脚本会检查参考目录的 `kernel32.dll` 文件版本为 `6.1.7601.*`，防止误用
Win10/11 的 DLL 得到虚假的 Win7 结论。不要把参考目录的系统 DLL 复制进
程序包。即使导入检查通过，动态加载接口、显卡驱动与桌面会话仍须实测。

## Win7 与较新 Windows 的验收

候选程序完成构建后，至少用一台 Win7 SP1 x64 和一台 Win10/11 x64 测试。
两台都在实际隔离局域网运行完整包，先用默认软件编解码配置。

| 检查 | 通过条件 |
| --- | --- |
| 启动 | 无缺失 DLL、找不到入口点或引擎初始化错误 |
| Win7 作为控制端 | 输入对方 IP，认证后自动出现首帧，鼠标键盘正常 |
| Win7 作为被控端 | Win10/11 连接 Win7，画面与输入正常 |
| 首帧与显示变化 | 不需要手动切换分辨率才能出图；切换显示器/分辨率后可恢复 |
| 会话恢复 | 断开、重连、注销后重新登录等使用场景按预期工作 |
| 常用功能 | 文本剪贴板与小文件传输正常 |
| Win10/11 回归 | 两台新系统间也能用本包建立正常会话 |

如仍出现“已连接，等待画面传输”，保留两端日志、系统版本、显卡型号、
连接方向及是否本地登录/锁屏的信息。这时应定位捕获、编码、传输还是解码
没有产出，不能仅凭网络连接成功判断显示链路正常。

## 本次实际验证记录（2026-11-05）

源码准备和依赖分析在 Linux 上执行，Windows 检查与编译由本仓库 Actions
执行。[构建 #5](https://github.com/longtutengfei/rustdesk-lan-win7/actions/runs/42048983180)
已成功，构建源码提交为 `4f8136ea2b93bef6a39a922b895da3fe956f4fc3`。
下载后已核对候选程序包 `rustdesk-lan-1.5.0-windows7-x64-candidate.zip`
的 SHA-256 与该次构建的来源记录一致：

```text
39b470bb629082a7bc632c85a9b96d0133b9cbf168a36000b0039e6eee5e1da0
```

此校验值对应候选程序包本身，不是 GitHub artifact 的外层 ZIP。
已取得以下结果：

| 检查 | 结果 | 能说明的范围 |
| --- | --- | --- |
| Rust 1.75.0、Windows target 的锁定依赖解析 | 通过 | Win7 配置中有 572 个可达包，toast 依赖已移除；默认配置仍保留 toast |
| 已声明 MSRV 的依赖 | 均不高于 1.75 | 另有 283 个包未声明 MSRV；本次已使用 Rust 1.75.0 通过 Windows 原生 release 编译 |
| Flutter 3.24.5 / Dart 3.5.4 的依赖解析 | 通过 | 独立锁文件可用 |
| 新隔离副本中强制锁定的离线依赖解析 | 通过 | 已下载缓存可重现解析，锁文件未被改写 |
| 原版 FRB codegen 1.80.1 与 Freezed 生成 | 通过 | 完整桥接代码生成成功 |
| 完整 Dart `lib` 分析 | 通过 | 0 个 error，0 个 warning，117 个现有 info 级提示 |
| UI 隔离、引擎下载/验证、CMake 交接、PE 检查器测试 | Windows 上 39 项通过 | 准备/下载测试 16 项，CMake/PE 测试 23 项；验证构建工具行为，不代表应用通过 Win7 实机测试 |
| 实际下载的兼容引擎 PE 预检 | 2 个 PE、0 个错误、0 个警告 | 仅引擎 DLL 与 `gen_snapshot.exe`，无 Win7 系统参考，状态为 `preflight-only` |
| 工作流 YAML 解析、`git diff --check` | 通过 | 配置可解析，无补丁空白错误 |
| 整个 Windows 应用编译、链接 | 构建 #5 通过 | Rust 原生 release 库与 Flutter Windows EXE 均已编译、链接成功 |
| 最终完整运行目录的 PE 导入检查 | 13 个 PE、0 个错误、0 个警告 | 未使用 Win7 系统参考 DLL 集，结果为 `preflight-only`；不能证明已通过 Win7 运行验收 |
| 候选 ZIP artifact 上传 | 构建 #5 成功 | 完整候选程序 ZIP 已上传至该次 Actions 运行，可下载后进行离线实机测试 |
| Win7 / Win10 / Win11 实机远控 | 未执行 | 尚无启动、IP 直连、画面传输的实机结论 |

工作流固定了 Rust、Flutter、引擎和依赖版本，但 Windows 2022 runner 中的
MSVC/Windows SDK 次版本仍会随镜像更新。本配置不是逐字节可重现构建声明；
最终二进制检查与目标系统运行测试仍是必要步骤。

## 参考资料

- [Rust 官方：从 1.78 起更新 Windows target 的最低系统要求](https://blog.rust-lang.org/2024/02/26/Windows-7/)
- [RustDesk 官方：让 Flutter 3.24.5 在 Windows 7 上运行](https://test.rustdesk.com/zh-cn/blog/ru-he-rang-flutter-3-24-yun-xing-yu-windows-7/)
- [本配置对应的 RustDesk 自定义引擎构建定义](https://github.com/rustdesk/engine/blob/9176a596b1f82625dfe8831edbcc4f2c9dc22da9/.github/workflows/flutter-engine-windows-x64-release-build.yml)
- [项目上游](https://github.com/tyaro/rustdesk-lan)
