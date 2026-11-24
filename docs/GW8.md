# Galaxy Watch 8 独立构建流程

Actions 中选择 **Build Galaxy Watch 8 BakaSU boot**，对应 `.github/workflows/build-boot-gw8.yml`。原来的 `build-boot.yml` 及 GW7 源码处理、配置、编译脚本保持原样。

输入和签名设置沿用现有流程：填写 Watch 8 原 boot 直链；源码链接按“完整基础包 → 型号 DTS 包 → 更新包”顺序，用英文分号分隔。BakaSU 仓库、commit、工具链地址可选。默认签名需要仓库 Secret `AVB_PRIVATE_KEY_B64`；不签名选项、签名及未签名镜像同时输出、LZ4 压缩均沿用当前仓库行为。只输出 boot，不生成独立 vbmeta.img。

本次错误发生于第二个包 `SM-L500_Opensource_dts.zip`，其中 `L500XXU2AZB1_kernel.tar.gz` 没有主内核，只有 `Kernel/exynos/device/erd/erd5535/` 和配套 `prebuilts/`。GW7 脚本只识别 `kernel/`，因此报告找不到内核源码。第三个包还有 `Kernel/kernel-6.6/`，也需要 GW8 的目录识别。

## GW8 专用文件

| 文件 | 处理内容 |
| --- | --- |
| `scripts/source_archives_gw8.py` | 独立解压与包识别；保留 `src/kernel-6.6/`、`src/exynos/`、`src/prebuilts/` 等原始布局。第二个包可仅含外部设备文件；第三个包分别覆盖 GKI 和设备目录 |
| `scripts/prepare_kernel_gw8.py` | 从 `build.config.common` 读取 Android 15 分支，从 `build.config.constants` 读取 clang，从 `exynos/soc-series/` 读取 SoC；确定外部 Kconfig 目录 |
| `scripts/compile_kernel_gw8.py` | 给配置和编译阶段传入 `KCONFIG_EXT_PREFIX`、`KCONFIG_EXT_SOC_PREFIX`、`KCONFIG_EXT_MODULES_PREFIX`；通过 `LOCALVERSION` 保持原内核 release，适配 Linux 6.6 不读取 `.scmversion` 的行为 |
| `tests/test_gw8.py` | 仅做小型压缩包、配置解析、编译环境和 workflow 路径的测试，不下载工具链或编译内核 |

这三个专用 Python 文件均在文件头标注 **Galaxy Watch 8 ONLY**。它们复用原脚本中的解压安全检查、文件复制、BakaSU 接入、配置修改、工具链下载、配置比较等函数，不修改这些原有函数。

继续共用的文件包括 `common.py`、`boot_image.py`、`module_trust.py`、`collect_results.py`、`avbtool.py`。因此签名、官方模块证书恢复/核验和镜像输出行为与当前仓库一致。

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
