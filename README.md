# Galaxy Watch BakaSU boot 云端编译

在 GitHub Actions 中下载原厂 boot 和三星开源包，按输入顺序合并源码，集成 BakaSU，编译并生成 `boot.img`。**只制作 boot，不生成单独的 vbmeta.img，不打包完整固件，不自动刷机。**

不同地区、尺寸、蓝牙/LTE 版本请提供各自匹配的原厂 boot 和源码。构建使用原 boot 内核中提取的完整配置，保留模板头部与三星尾部；没有写死 SM-L315F、fresh7ulxx 或某个地区的设备树。SoC、内核版本、clang 版本从用户输入的源码与 boot 读取。

## 1. 配置签名密钥

默认构建会使用 `SHA256_RSA4096` 对 **boot 自身**添加 AVB 签名。这里的 AVB 信息位于 boot 内部；不会输出 `vbmeta.img`。

在仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret** 添加：

```text
Name: AVB_PRIVATE_KEY_B64
Secret: 你的 RSA-4096 私钥 PEM 文件的 Base64 文本
```

Action 将这个 Secret 以同名环境变量提供给签名步骤。不要把私钥放入普通 Variables、workflow 输入框或提交到仓库。

已有私钥时，在 PowerShell 生成 Base64 并复制到剪贴板：

```powershell
$keyBytes = [System.IO.File]::ReadAllBytes((Join-Path ([Environment]::GetFolderPath('UserProfile')) '.gw-signing/gw-avb-rsa4096.pem'))
[Convert]::ToBase64String($keyBytes) | Set-Clipboard
```

Linux / WSL：

```bash
base64 -w 0 /path/to/gw-avb-rsa4096.pem
```

没有密钥时，可在本机生成并自行保存：

```bash
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out gw-avb-rsa4096.pem
base64 -w 0 gw-avb-rsa4096.pem
```

私钥必须是未加密的 PEM RSA-4096 私钥。本地签名与三星生产签名是不同的；签名通过表示镜像内容与该密钥相符。

## 2. 运行编译

把本仓库文件推送到 GitHub 默认分支后，打开：

**Actions → Build Galaxy Watch BakaSU boot → Run workflow**。

| 输入 | 填写方式 |
| --- | --- |
| `boot_url` | 对应系统的原厂 boot 下载直链；接受 `.img`、`.img.lz4`、`.img.gz` 内容。下载地址无需以这些后缀结尾 |
| `source_urls` | 三星源码包下载直链，用英文分号 `;` 分隔。先完整基础包，再型号适配包，再更新包；允许三个或更多，也可以只有一个完整包 |
| `bakasu_repository` | 可选；`owner/repo` 或 HTTPS Git 地址。留空使用 `https://github.com/Baka-SU/BakaSU.git` |
| `bakasu_ref` | 可选；commit 哈希、分支或 tag。留空使用所选仓库**默认分支的最新提交** |
| `unsigned` | 默认不勾选。勾选“不签名（不推荐）”后，不生成新的 boot AVB，也不复制旧 boot AVB；**保留原厂三星尾部和恢复的官方模块证书，并补零至原 boot 大小**。无需签名 Secret |
| `toolchain_url` | 可选；clang 工具链压缩包直链。留空根据源码的 `CLANG_VERSION` 与 Android 分支下载 AOSP 工具链 |

示例：

```text
boot_url:
https://example.com/stock-boot.img

source_urls:
https://example.com/SM-L305U_Opensource.zip;https://example.com/SM-L315F_EUR_Opensource_dts.zip;https://example.com/BZF4-update.zip
```

URL 必须在云端无需登录就能直接下载文件；网盘预览页、需要会话 Cookie 的下载页不能作为直链。保留 URL 中的查询参数；构建报告会去掉查询字符串，避免把临时下载令牌写入 artifact。

## 3. 自动解压与覆盖规则

支持 ZIP、tar、tar.gz/tgz、tar.xz、tar.bz2，以及压缩包中继续嵌套的 ZIP/tar。每个链接按填写顺序处理；同一总包中的内嵌源码包按 ZIP/tar 成员顺序处理。

以下以一组三星源码包为例：

```text
SM-L305U_Opensource.zip
└── L305USQU3BZB4_Kernel.tar.gz
    ├── kernel/                 → work/build/src/kernel/
    ├── projects/               → work/build/src/projects/
    └── prebuilts/              → work/build/src/prebuilts/

SM-L315F_EUR_Opensource_dts.zip
└── L315FXXU3BZB4_kernel.tar.gz
    └── Kernel/
        ├── kernel/             → 覆盖相同的 src/kernel/
        └── prebuilts/          → 覆盖相同的 src/prebuilts/

BZF4-update.zip
└── L305USQS3BZF4_kernel.tar.gz
    └── Kernel/kernel/          → 继续覆盖相同的 src/kernel/
```

会自动定位外层目录中的内核根目录，保留旁边的 `projects/`、`prebuilts/` 和源码内部相对软链接。已识别为内核树后，不会继续解压源码里的测试用压缩包。第一个实际源码包必须是完整内核树；后续可以是增量目录。若同一个包中出现多个并列内核树，脚本会列出路径并停止，避免选错。

所有下载内容均记录 SHA-256；每次覆盖的同路径文件、前后哈希记录在 `source-manifest.json`。

## 4. 编译过程中做的修改

1. 从原 boot 提取 ARM64 `Image`、内嵌 IKCONFIG、原内核 release 和自签名内核模块公开证书，自动恢复模块信任。
2. 克隆完整 BakaSU Git 历史，解析选择的 ref，打印并保存完整 commit 哈希。
3. 添加 `drivers/kernelsu -> ../BakaSU/kernel`，在 `drivers/Makefile` 和 `drivers/Kconfig` 加入编译入口。
4. 基于原内核配置启用 `KSU`、`KSU_TRACEPOINT_HOOK`、`KSU_MULTI_MANAGER_SUPPORT`；关闭 manual hook、SUSFS，以及 UH/RKP/KDP/KDP_CRED/DEFEX；将恢复的官方模块公开证书加入 `SYSTEM_TRUSTED_KEYS`。
5. 安装 Ubuntu 编译依赖，下载匹配 clang，执行 `olddefconfig`、`prepare`、`Image modules`，保持原内核 release。原 ARM32 compat、模块保护和 SELinux 配置会被保留；内存较小的 runner 会自动补充 4 GiB 交换空间供 BTF 阶段使用。
6. 替换原 boot 的内核，更新 `kernel_size`，保留原头部其余字段和三星尾部。
7. 编译后从 `vmlinux` 的 `__cert_list_start/end` 符号读取实际内置信任列表，强制核对原厂模块证书确实位于信任列表及 Image 中。缺失时停止，不输出 boot。
8. 默认签名：添加 boot 内部 AVB hash/footer，使用 Secret 的 RSA-4096 私钥签名，并验签、校验 payload。勾选不签名则只在替换后的内容末尾补零至原 boot 大小，不添加 AVB。两条路径都逐字节保留三星尾部，并再次核对最终 boot 内核中的官方模块证书。

### 内核替换、大小与对齐

镜像按下面的结构重建：

```text
原 boot 前 4,096 字节头部（只更新 kernel_size）
+ 本次构建的完整 Image（替换原内核 payload）
+ 原内核之后、AVB 之前的三星尾部及原有填充
+ 新的 AVB/分区填充（签名）或纯零填充（不签名）
```

不会把整份原 boot 与新 Image 或另一份 boot 拼接。最终验证器独立检查实际输出：偏移 4,096 开始的内核逐字节等于构建的 Image；其后的尾部逐字节等于原尾部；头部只有 `kernel_size` 字段允许变化；最终文件大小必须等于原 boot，且是 4 KiB 的整数倍。未签名版本中，保留尾部之后必须全为零，不允许残留旧 AVB。

本例原 boot 和两种最终输出均为 `67,108,864` 字节。官方内核 payload 的末端本身不在 4 KiB 边界；原尾部开头的 `1,536` 字节填充让 `SEANDROIDENFORCE` 位于 4 KiB 边界。脚本保留这段填充并核对标记对齐，不会擅自把 Image 末端当成必须对齐的位置。

### 不签名时保留的内容

“不签名”只控制 **boot 的 AVB 重签名**，不会关闭内核模块签名保护，也不会删除三星尾部或模块信任证书：

- 原 boot 内核之后的三星尾部材料（本例为 2,336 字节，包含 `SEANDROIDENFORCE`）逐字节保留。
- 原厂 GKI 模块公开证书从原内核恢复，重新编入新内核的实际内置信任列表；签名和不签名构建均强制检查。
- 原厂 `.ko` 驱动的签名附在模块文件自身。此流程只输出 boot，不改写 vendor_boot、vendor_dlkm 或 system_dlkm 中的原厂模块及其签名。
- 原 boot 的旧 AVB hash、签名块和 footer 会去掉，因为它们对应旧内核；未签名输出的剩余空间全部补零，不声称通过旧 AVB 验证。原样保留的三星尾部也不等于三星为新内核重新签名。

本流程不需要提供原厂模块私钥；从原 boot 恢复的是公开证书。Action 不获取完整 AP、vendor_boot 或 super，因此不会重复历史本地构建对 258 个原厂模块的完整 ABI/签名核查。

默认最新版 BakaSU 会随上游更新；某次构建需要重现时，把报告里的完整 commit 填进 `bakasu_ref`。最新源码和成功启动之间仍需设备验证。

## 5. 下载结果

完成后从该次运行的 **Artifacts** 下载：

```text
gw-boot-<BakaSU commit前12位>-signed或unsigned-<run id>
```

其中包括：

- `boot.img`：最终镜像。
- `Image`、`kernel.config`、`Module.symvers`、`System.map`：编译结果及核查材料。
- `bakasu.json`、`prepare.json`、`compile.json`、`module-trust.json`、`boot-manifest.json`、`config-check.json`、`source-manifest.json`：来源、完整 BakaSU commit、工具链、配置、原厂模块信任和镜像校验记录。
- `SHA256SUMS`、`BUILD.txt`：文件哈希与摘要。

失败时也会上传已完成的公开报告，帮助定位下载、合并、配置或编译问题。AVB 私钥和自动生成的内核模块签名私钥不会上传。没有自动发布 Release、定时更新或刷写步骤。

## 本地脚本测试

```bash
sudo apt-get install python3 python3-cryptography python3-lz4 python3-pyelftools openssl git gcc
python3 -m unittest discover -s tests -v
```

测试覆盖嵌套解压、覆盖顺序、子目录识别、目录穿越拒绝、相对软链接、BakaSU 默认分支/指定 commit、签名验签、纯未签名输出、官方模块证书保留，以及实际内置信任列表的正反例核查。

`scripts/avbtool.py` 来自 AOSP，保留原 MIT 许可头；它与本地已核验的 BZF4 打包工具一致。

## 已执行的本地验证

2026-11-23 使用真实 SM-L315F BZF4 原 boot、上述三个三星源码 ZIP 和上游默认分支提交 `63ad1ea3a90455dee9d77163cdafb028d8a90416`，完成源码自动合并、配置恢复、`Image modules` 全量编译、boot 打包和 RSA-4096 签名验签。生成的配置与历史成功构建一致。

本地还使用保留的原厂模块完成额外核查：258 个模块，14,422 条符号 CRC，0 个不一致、0 个未解析符号；60 个签名模块全部受新内核信任。这项额外核查使用了本机保存的 vendor_boot 和模块提取目录，不属于只输入 boot/源码链接的云端流程。

34 项脚本测试及 actionlint 1.7.12 语法检查通过。用历史 Image 与历史签名 salt 重打包时，也重现了已知成功 boot 的完整 SHA-256。默认新构建使用 Image SHA-256 作为 salt。补充检查确认：两种输出均为 64 MiB 且整体 4 KiB 对齐；未签名 boot 只增加零填充，三星尾部与原厂逐字节一致，官方模块证书同时位于编译内核的真实信任列表和最终 boot 内核中。测试同时覆盖整份 boot 错误拼接的拒绝、新内核缩小时清除旧内核残留、尾部填充及 AVB 残留拒绝。

这次本地生成的签名 boot SHA-256 为 `bad177b496fbffe12a0c72b4f4337cc2881d625447f47e4b2a96ea0487c6f917`。这份新构建完成了文件和模块核查，尚未刷入手表；本次也未在 GitHub runner 实际运行。
