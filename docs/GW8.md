# Galaxy Watch 8 独立构建流程

Actions 中选择 **Build Galaxy Watch 8 BakaSU boot**，对应 `.github/workflows/build-boot-gw8.yml`。GW7 使用独立的 `build-boot-gw7.yml`；两套源码布局适配分别维护。

输入和签名设置沿用现有流程：填写 Watch 8 原 boot 直链；源码链接按“完整基础包 → 型号 DTS 包 → 更新包”顺序，用英文分号分隔。BakaSU 仓库、commit、工具链地址可选。默认签名需要仓库 Secret `AVB_PRIVATE_KEY_B64`；不签名选项、签名及未签名镜像同时输出、LZ4 压缩均沿用当前仓库行为。只输出 boot，不生成独立 vbmeta.img。

本次错误发生于第二个包 `SM-L500_Opensource_dts.zip`，其中 `L500XXU2AZB1_kernel.tar.gz` 没有主内核，只有 `Kernel/exynos/device/erd/erd5535/` 和配套 `prebuilts/`。GW7 脚本只识别 `kernel/`，因此报告找不到内核源码。第三个包还有 `Kernel/kernel-6.6/`，也需要 GW8 的目录识别。

## GW8 专用文件

| 文件 | 处理内容 |
| --- | --- |
| `scripts/gw8/source_archives.py` | 独立解压与包识别；保留 `src/kernel-6.6/`、`src/exynos/`、`src/prebuilts/` 等原始布局。第二个包可仅含外部设备文件；第三个包分别覆盖 GKI 和设备目录 |
| `scripts/gw8/prepare_kernel.py` | 从 `build.config.common` 读取 Android 15 分支，从 `build.config.constants` 读取 clang，从 `exynos/soc-series/` 读取 SoC；确定外部 Kconfig 目录 |
| `scripts/gw8/compile_kernel.py` | 给配置和编译阶段传入外部 Kconfig 环境；调用共用版本处理保留原 boot 的完整 release，适配 Linux 6.6 不读取 `.scmversion` 的行为 |
| `scripts/gw8/abi_symbols.py` | 根据输入源码 `BUILD.bazel` 中 GKI 目标的主 KMI 列表及附加 filegroup，生成原配置要求的原始 ABI 符号列表，补齐直接调用 make 时缺少的 Kleaf 步骤 |
| `tests/test_gw8.py` | 仅做小型压缩包、配置解析、编译环境和 workflow 路径的测试，不下载工具链或编译内核 |
| `tests/test_abi_symbols_gw8.py` | 用小型符号列表与模拟 make 验证 ABI 列表合并、保护配置保留和编译调用顺序，不运行编译器 |

这四个专用脚本均在文件头标注 **Galaxy Watch 8 ONLY**。`scripts/` 根目录只放跨机型脚本：`archive_utils.py`（安全解压与文件合并）、`kernel_prepare.py`（BakaSU、配置和工具链准备）、`kernel_build.py`（配置校验），以及版本、镜像、结果和证书处理脚本。`scripts/gw7/` 和 `scripts/gw8/` 只放对应机型的源码布局、准备和编译入口；调用时无需在文件名中重复 `gw7` 或 `gw8`。

继续共用的文件包括 `kernel_release.py`、`common.py`、`boot_image.py`、`module_trust.py`、`collect_results.py`、`avbtool.py`。因此版本重建、签名、官方模块证书恢复/核验和镜像输出行为保持一致。

## ABI 符号列表

原 boot 配置中的 `CONFIG_UNUSED_KSYMS_WHITELIST="abi_symbollist.raw"` 指向构建时生成的文件。三星原 Kleaf 流程会合并 GKI 主符号列表和附加厂商列表，再展平成排序、去重的原始符号名。直接执行 `make Image modules` 时必须先补齐这一步，否则会报 `No rule to make target 'abi_symbollist.raw'`。

GW8 编译脚本在 `olddefconfig` 和配置核对后、`make prepare` 前完成生成。列表路径取自这次输入源码的 GKI 构建定义，输出位置取自这次内核配置；不会写死固件版本或在线下载另一版本的符号列表。兼容带 `[abi_symbol_list]`／`[abi_whitelist]` 分节头的 libabigail 列表，以及 `abi_gki_aarch64_tuxera` 这种无分节头、每行一个符号的纯文本列表；逐文件独立解析后合并，保留大小写，排序去重，支持注释、空行、CRLF 和 UTF-8 BOM。`CONFIG_MODULE_SIG_PROTECT`、`CONFIG_TRIM_UNUSED_KSYMS` 和原厂模块证书保持原有处理。缺少声明的源列表、符号为空或遇到尚未适配的动态 BUILD 表达式时提前报错，不用空文件绕过。

额外的 `abi-symbols-gw8.json` 会随 artifact 上传，记录所用 GKI 目标、每份源列表路径、格式、符号数及 SHA-256，以及输出符号数和 SHA-256，方便后续核对。共用收集脚本仅增加了对此可选报告的复制；GW7 不调用 ABI 生成脚本。

## 版本号与系统更新

GW7、GW8 均不固定 boot 固件版本、基础内核版本、Android/KMI 版本或页面大小。它们从输入 boot 提取完整 Linux release 和 `CONFIG_LOCALVERSION`，从源码 Makefile 读取基础版本，并核对二者匹配。

例如 `6.6.89-android15-8-abL500XXS2AZF4-4k` 中，原配置为 `CONFIG_LOCALVERSION="-4k"`。共用版本脚本会把 `-android15-8-abL500XXS2AZF4` 写入**输出目录**的 `localversion.gw-boot`，使内核版本按“基础版本 → Android/固件标识 → 原配置后缀”排列；不把它拼成 `6.6.89-4k-android15-8-...`，也不修改原配置中的页面大小。

原配置后缀在前的旧版本也会按输入顺序还原。以后更新系统，只需提供相互匹配的新 boot 和源码；Android/固件标识和页面大小均动态读取，最终 `kernelrelease` 仍须与输入 boot 完全一致。源码布局或 BakaSU 上游出现其他变化时可能仍需适配。

## 这组三个链接

```text
https://archive.org/download/samsung-opensource-14123/SM-L325U_Opensource.zip;https://archive.org/download/samsung-opensource-14123/SM-L500_Opensource_dts.zip;https://archive.org/download/samsung-opensource-14123/SM-L325U_Opensource_L335NKOS3AZF4_L335USQS3AZF4_L500XXS2AZF4_L500ZCS2AZF4_L5050ZCS3AZF4_L505FXXS3AZF4_L505NKOS3AZF4_L505USQS3AZF4.zip
```

对应布局：

```text
src/
├── kernel-6.6/                       # GKI；6.6.89
│   └── drivers/usb/gadget/function/ # 更新包覆盖 android_f_accessory.c
├── kernel/tests/                     # 基础包中的独立测试资料，保持原位置
├── exynos/
│   ├── device/erd/erd5535/           # SM-L500 型号 DTS、配置及后续设备更新
│   ├── soc-series/s-wear6/           # EXYNOS_SOC=s5e5535
│   └── external-modules/            # 外置模块 Kconfig
└── prebuilts/                       # 配套固件
```

`kernel-6.6/` 与基础包中的 `kernel/tests/` 是两个不同目录，GW8 流程不会把它们折叠成一棵 `src/kernel/`。工作目录为 `work/gw8-build`，产物为 `dist/gw8`，artifact 名称以 `gw8-` 开头。

本次只做源码布局、配置和 workflow 适配及轻量验证，不在本机运行内核编译。云端全量编译和设备启动结果待实际测试。
