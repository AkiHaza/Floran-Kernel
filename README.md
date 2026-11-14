# Floran Kernel

基于 GitHub Actions 的 Android 6.1 GKI 内核自动构建工作流，面向 `sm8650` / `pineapple` 内核源码及其 Oplus GKI 配置。

工作流会拉取指定 ROM 的内核源码，按需集成 KernelSU、SUSFS、Droidspaces、网络功能等组件，最终通过 AnyKernel3 打包为可刷写的 ZIP；也可自动创建 GitHub Release。

> [!WARNING]
> 内核与模块源码、ROM、设备及固件版本必须相互匹配。刷写内核存在无法启动、数据丢失或其他不可预期风险，请自行确认兼容性并提前备份数据。

## 功能特性

- 支持多个 ROM 内核源码：
  - YAAP（Android 16 / 17）
  - LineageOS（Android 16 / 17）
  - FrierenKernel（Android 17，LineageOS 优化版）
  - crDroid（Android 16 / 17）
  - PixelOS
  - DerpFest（Android 17）
  - AOSPA（Android 17）
- 支持多种 Root 内核方案：
  - KernelSU Official
  - KernelSU-Next
  - BakaSU
  - 上述方案的 SUSFS 变体（部分组合）
  - 不集成 KernelSU
- 可选功能：
  - BBR、ECN 与 FQ 队列调度
  - IPSet 与 IPv6 NAT
  - Droidspaces 容器支持
  - Droidspaces Extended：额外启用虚拟 HCI、systemd-coredump 相关配置及 Lindroid EVDI DRM
  - Docker 容器支持（单一开关，默认关闭；⚠️ 部分机型有 bootloop 风险，详见 `patch/docker.config` 头注释）
- 使用 AOSP Clang 编译：Android 17 源码使用 `clang-r596125`，Android 16 源码使用 `clang-r563880c`
- 通过 AnyKernel3 输出可刷写 ZIP
- 支持上传 Actions Artifact，并可自动创建 GitHub Release

## 使用方法

### 单独构建

1. Fork 本仓库。
2. 打开仓库的 **Actions** 页面。
3. Android 16 内核选择 **Build Android16 Kernel**；Android 17 内核选择 **Build Android17 Kernel**。
4. 点击 **Run workflow**，按需填写构建参数。
5. 等待工作流完成：
   - 可在对应工作流运行页面的 **Artifacts** 下载 ZIP；
   - 若启用了 `Create GitHub Release`，可在仓库的 **Releases** 页面下载 ZIP。

### 构建参数

| 参数 | 说明 |
|---|---|
| `Build Android16 Kernel` | 选择并构建 `YAAP`、`LineageOS`、`Crdroid` 或 `PixelOS` 内核；该工作流提供 Droidspaces 等通用选项。 |
| `Build Android17 Kernel` | 选择并构建 `YAAP`、`DerpFest`、`LineageOS`、`FrierenKernel`、`Crdroid` 或 `AOSPA` 内核；Android 17 工作流固定使用 `clang-r596125`，FrierenKernel 的可选增强仅保留 Droidspaces。 |
| `ROM Kernel Source Code` / `Android 17 Kernel Source Code` | 在对应 Android 版本的工作流中选择内核源码；ROM 选项不带版本后缀，由工作流内部的 `android_version` 区分源码分支与工具链。 |
| `KernelSU Version` | 选择 KernelSU 集成方案，或选择 `None` 构建非 KernelSU 内核。 |
| `ksu_branch` | 单独构建时指定所选 KernelSU 方案的源码分支；留空使用默认值。例如 BakaSU 可填写 `sync-up`。 |
| `bakasu_branch` / `kernelsu_next_branch` | 矩阵构建中分别覆盖 BakaSU、KernelSU-Next 的源码分支，互不影响；留空使用各自默认值。 |
| `susfs_dev` | 布尔开关，默认关闭；开启使用 6.1 的 `gki-android14-6.1-dev` 分支，关闭使用 `gki-android14-6.1` 稳定分支。仅带 `susfs` 的方案使用此参数。 |
| `Enable IPSET & IPv6_NAT` | 启用 IPSet、IPv6 NAT 及相关 Netfilter 配置；FrierenKernel 忽略此选项。 |
| `Enable BBR & ECN` | 启用 BBR、ECN 与 FQ；FrierenKernel 忽略此选项。 |
| `Droidspaces Container Support` | 选择 `none`、`standard` 或 `extended` 容器支持。YAAP 不应用 Droidspaces 补丁。 |
| `Docker Container Support` | ⚠️ DANGER：追加 `patch/docker.config` 单一片段（namespaces/cgroup v2/USER_NS/overlay/btrfs），可能导致部分机型 bootloop。 |
| `Custom Kernel Name` | 设置内核附加版本名。脚本会自动补上 `-` 前缀。 |
| `创建 GitHub Release？` | 是否在构建成功后创建并上传 GitHub Release。 |

## ROM 源码与分支

单独构建工作流使用以下上游与分支：

矩阵构建工作流构建 `YAAP-17`、`FrierenKernel`、`Crdroid-17`、`LineageOS` 与 `AOSPA`；其中 YAAP-17、FrierenKernel、Crdroid-17 和 AOSPA 使用 Android 17 的 `clang-r596125`，LineageOS 使用 Android 16 的 `clang-r563880c`。矩阵不再构建 crDroid 16，仍可通过单独构建工作流选择。YAAP-17 跳过 Droidspaces 与通用性能优化，FrierenKernel 跳过 BBR/ECN/FQ、IPSet/IPv6 NAT 和通用性能优化，但保留 Droidspaces 选项。

Android 17 使用的 Clang 工具链产物：[`clang-r596125.tar.gz`](https://github.com/AkiHaza/Floran-Kernel/releases/download/toolchain-r596125/linux-x86-refs_heads_android17-release-clang-r596125.tar.gz)

| Android 版本 | ROM 源码 | Kernel 仓库 / 分支 | Modules 仓库 / 分支 |
|---|---|---|---|
| 16 | YAAP | [`AkiHaza/android_kernel_oneplus_sm8650`](https://github.com/AkiHaza/android_kernel_oneplus_sm8650) / `sixteen` | [`AkiHaza/android_kernel_oneplus_sm8650-modules`](https://github.com/AkiHaza/android_kernel_oneplus_sm8650-modules) / `sixteen` |
| 17 | YAAP | [`AkiHaza/android_kernel_oneplus_sm8650`](https://github.com/AkiHaza/android_kernel_oneplus_sm8650) / `seventeen` | [`AkiHaza/android_kernel_oneplus_sm8650-modules`](https://github.com/AkiHaza/android_kernel_oneplus_sm8650-modules) / `seventeen` |
| 16 | LineageOS | [`LineageOS/android_kernel_oneplus_sm8650`](https://github.com/LineageOS/android_kernel_oneplus_sm8650) / `lineage-23.2` | [`LineageOS/android_kernel_oneplus_sm8650-modules`](https://github.com/LineageOS/android_kernel_oneplus_sm8650-modules) / `lineage-23.2` |
| 17 | LineageOS | [`LineageOS/android_kernel_oneplus_sm8650`](https://github.com/LineageOS/android_kernel_oneplus_sm8650) / `lineage-24.0` | [`LineageOS/android_kernel_oneplus_sm8650-modules`](https://github.com/LineageOS/android_kernel_oneplus_sm8650-modules) / `lineage-24.0` |
| 17 | FrierenKernel | [`AkiHaza/custom_kernel_oneplus_sm8650`](https://github.com/AkiHaza/custom_kernel_oneplus_sm8650) / `lineage-24.0` | [`AkiHaza/custom_kernel_oneplus_sm8650-modules`](https://github.com/AkiHaza/custom_kernel_oneplus_sm8650-modules) / `lineage-24.0` |
| 16 | Crdroid | [`crdroidandroid/android_kernel_oneplus_sm8650`](https://github.com/crdroidandroid/android_kernel_oneplus_sm8650) / `16.0` | [`crdroidandroid/android_kernel_oneplus_sm8650-modules`](https://github.com/crdroidandroid/android_kernel_oneplus_sm8650-modules) / `16.0` |
| 17 | Crdroid | [`crdroidandroid/android_kernel_oneplus_sm8650`](https://github.com/crdroidandroid/android_kernel_oneplus_sm8650) / `17.0` | [`crdroidandroid/android_kernel_oneplus_sm8650-modules`](https://github.com/crdroidandroid/android_kernel_oneplus_sm8650-modules) / `17.0` |
| 16 | PixelOS | [`PixelOS-Devices/android_kernel_oneplus_sm8650`](https://github.com/PixelOS-Devices/android_kernel_oneplus_sm8650) / `sixteen-qpr2` | [`PixelOS-Devices/android_kernel_oneplus_sm8650-modules`](https://github.com/PixelOS-Devices/android_kernel_oneplus_sm8650-modules) / `sixteen-qpr2` |
| 17 | DerpFest | [`ppanzenboeck/android_kernel_oneplus_sm8650`](https://github.com/ppanzenboeck/android_kernel_oneplus_sm8650) / `lineage-23.2` | [`ppanzenboeck/android_kernel_oneplus_sm8650-modules`](https://github.com/ppanzenboeck/android_kernel_oneplus_sm8650-modules) / `derp16.2-arb` |
| 17 | AOSPA | [`AkiHaza/AOSPA_kernel_oneplus`](https://github.com/AkiHaza/AOSPA_kernel_oneplus) / `standalone` | [`AkiHaza/AOSPA_kernel_oneplus_modules`](https://github.com/AkiHaza/AOSPA_kernel_oneplus_modules) / `standalone` |

在 **Build Android17 Kernel** 中，DerpFest、LineageOS 与 AOSPA 可选择 `standard` 或 `extended` Droidspaces 支持。YAAP 会忽略此选项。FrierenKernel 保留所选 Root/SUSFS 方案，额外增强仅保留 Droidspaces；即使勾选，也会跳过 BBR/ECN/FQ、IPSet/IPv6 NAT 及工作流追加的通用性能配置。

AOSPA 的模块源码放在内核目录内的 `sm8650/sm8650-modules`，与其独立构建版 Makefile 的目录约定一致。选择 `None` 时，工作流会移除源码预置的 KernelSU Kconfig/Makefile 引用，避免未初始化的 KernelSU 子模块阻断配置生成；已有 KernelSU 源码及子模块声明会保留。当前 ZIP 仅替换内核 `Image`，不会重新构建或替换设备现有模块，因此仍须匹配对应 ROM 的模块 ABI。

## KernelSU 与 SUSFS

可选择下列 KernelSU 方案：

| 选项 | 内容 |
|---|---|
| `KernelSU-Official` | 官方 KernelSU |
| `KernelSU-Official-susfs` | 官方 KernelSU + SUSFS |
| `KernelSU-Next` | KernelSU-Next |
| `KernelSU-Next-susfs` | KernelSU-Next + SUSFS |
| `BakaSU` | BakaSU |
| `BakaSU-susfs` | BakaSU + SUSFS |
| `None` | 不集成 KernelSU |

单独构建可通过 `ksu_branch` 覆盖所选方案的分支。例如选择 `BakaSU-susfs` 并填写 `ksu_branch=sync-up`，即可使用 BakaSU 的 `sync-up` 分支。矩阵构建通过 `bakasu_branch` 和 `kernelsu_next_branch` 分别设置两个方案，避免将 BakaSU 的分支名套用到 KernelSU-Next。留空时，KernelSU Official、BakaSU 使用 `main`；KernelSU-Next 使用现有 pershoot 仓库的 `dev-susfs`。选择 `None` 时忽略分支输入。

工作流先克隆指定分支，再执行该分支的 `kernel/setup.sh`，并核对最终分支、提交及驱动链接；分支不存在或安装后切换到其他分支时会终止构建。KernelSU 保留完整提交历史，以便正确计算版本号；实际分支和提交会写入 Actions 日志与摘要。

选择带 `susfs` 的方案时，工作流固定使用 [simonpunk/susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu) 的 `android14-6.1` 分支系列。`susfs_dev` 只控制是否使用 dev 分支，不支持填写自定义分支名；ROM 的 Android 16 / 17 版本不影响此选择。

| `susfs_dev` 开关 | SUSFS 分支 |
|---|---|
| 关闭 / `false`（默认） | `gki-android14-6.1` |
| 开启 / `true` | `gki-android14-6.1-dev` |

例如单独构建填写 `ksu_branch=sync-up` 并勾选 `susfs_dev`，或矩阵构建填写 `bakasu_branch=sync-up` 并勾选 `susfs_dev`。稳定版与 dev 版均使用补丁文件 `50_add_susfs_in_gki-android14-6.1.patch`，文件名不附加 `-dev`；官方 SUSFS 方案与其他方案使用同一开关。

所有 SUSFS 方案均通过 `.github/scripts/apply-susfs-kernel.py` 在临时目录验证并应用内核补丁，兼容 YAAP 的栈上 `linux_binprm`、`bprm_execve` 参数差异及 `fs/namespace.c`、`fs/super.c` 的声明上下文。完整 hook 校验通过才写回，未知冲突会直接终止构建，不会丢弃功能补丁。矩阵构建使用同一处理器。

SUSFS 的凭据切换辅助函数与其调用方统一受 `CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER` 控制，避免 BakaSU 尚未提供此可选功能时触发未使用函数错误；保留内核 `Werror` 检查。兼容补丁在源码布局未知或不完整时会终止构建。

`KernelSU-Official-susfs` 通过统一入口 `.github/scripts/integrate-susfs.sh` 的 `source/config/built` 三个阶段完成集成及校验：检查并应用上游 `KernelSU/10_enable_susfs_for_ksu.patch`，补齐官方 KernelSU 侧的配置及接口，并记录两端源码提交。内核补丁在临时目录验证全部新增片段后才写回，仅适配已知的 `fs/exec.c` 参数／栈对象差异及 namespace/super 声明上下文；任何未解决的功能冲突都会终止构建并保留 reject。

仅官方 SUSFS 方案在合并 vendor 配置后核实最终 `out/.config`，编译后检查 `fs/susfs.o`、`vmlinux`、`Image` 与 `System.map` 的关键静态键符号，校验通过才打包，并上传 `SUSFS_Evidence_*`（最终配置及符号表）。上述检查不替代刷机后的运行验证。

刷入集成 KernelSU 的内核后，请安装与所选方案相对应的管理器：

- [KernelSU](https://github.com/tiann/KernelSU)
- [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next)
- [BakaSU](https://github.com/Baka-SU/BakaSU)

## Droidspaces 容器支持

| 模式 | 内容 |
|---|---|
| `none` | 不添加 Droidspaces 相关功能。 |
| `standard` | 启用 PID、IPC、挂载命名空间、SysV IPC、POSIX 消息队列、NTSync 等基础容器支持。 |
| `extended` | 在标准支持基础上，额外启用虚拟 HCI、Lindroid EVDI DRM，以及相关扩展配置。 |

> [!NOTE]
> Droidspaces 补丁支持 DerpFest 等非 YAAP 源码，不会应用于 YAAP。

## 输出文件命名

单独构建的 ZIP 大致遵循：

```text
<ROM 源码>-A<Android 版本>-<KSU 方案>[-dss|-dss-ext][-docker]-<UTC 月日>.zip
```

其中：

- `A16` / `A17`：对应工作流的 Android 版本；Actions Artifact 与 Release 也会显示版本，便于区分同名 ROM。
- `dss`：Droidspaces Standard
- `dss-ext`：Droidspaces Extended
- `docker`：启用 Docker 容器支持

## 本地构建

工作流使用的主要依赖如下：

```bash
sudo apt update
sudo apt install -y \
  bc bison flex libssl-dev libelf-dev libdw-dev build-essential \
  lz4 git python3 curl dwarves cpio gcc-aarch64-linux-gnu
```

编译 Android 17 内核时使用 Release 中的 `clang-r596125` 工具链产物，Android 16 内核使用 `clang-r563880c`，并执行：

```bash
make O=out gki_defconfig vendor/pineapple_GKI.config vendor/oplus/pineapple_GKI.config
make -j"$(nproc)" O=out Image
```

完整的源码拉取、补丁应用、KernelSU/SUSFS 集成与打包步骤，请以 [`.github/workflows/build-android16.yml`](.github/workflows/build-android16.yml)（Android 16）和 [`.github/workflows/build-android17.yml`](.github/workflows/build-android17.yml)（Android 17）为准；两者共享 [`.github/workflows/_build-kernel.yml`](.github/workflows/_build-kernel.yml) 中的构建逻辑。

## 致谢

- [AnyKernel3](https://github.com/AkiHaza/AnyKernel3)
- [KernelSU](https://github.com/tiann/KernelSU)
- [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next)
- [BakaSU](https://github.com/Baka-SU/BakaSU)
- [SUSFS4KSU](https://gitlab.com/simonpunk/susfs4ksu)
- 各 ROM、内核源码及补丁项目的维护者
