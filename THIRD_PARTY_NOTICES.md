# 第三方许可说明

本仓库原创构建脚本、测试、工作流和文档适用根目录的 GNU GPL v3 许可证。以下随仓库提供的第三方文件保留其自身许可。

## Android Verified Boot avbtool

- 文件：`scripts/avbtool.py`
- 项目：Android Open Source Project，`platform/external/avb`
- 上游地址：<https://android.googlesource.com/platform/external/avb/>
- 版权：Copyright 2016, The Android Open Source Project
- 许可证：MIT，原完整许可头保留在文件开头。

```text
Copyright 2016, The Android Open Source Project

Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 构建时下载的内容

三星内核源码包、原 boot、BakaSU Git 仓库、clang 工具链和 Ubuntu/Python 依赖由工作流运行时取得，不作为本仓库源码的一部分分发。各自的版权和许可证保持不变；分发构建产物时，应遵守相应内容的许可证要求。
