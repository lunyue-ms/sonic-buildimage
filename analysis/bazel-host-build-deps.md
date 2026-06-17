# Host-direct Bazel build — 安装的依赖记录

> 目的：记录"在 host 上直接跑 `bazel build //dockers/docker-orchagent:load`"
> （**不**套 `sonic-bazel-builder` 容器）所需安装的全部包，方便追溯装了什么、为什么装。
>
> 对应 workflow：`.github/workflows/bazel-orchagent-host.yml`
> 对照（容器版）：`tools/bazel-builder/Dockerfile` + `.github/workflows/bazel-orchagent.yml`
>
> 每次在 host 直跑路径里新增/删除包，请同步更新本文件。

## 适用前提（gotcha）

| 前提 | 说明 |
|------|------|
| **glibc ≥ 2.36** | 上游 Bazel rules 会拉 Debian bookworm 的 libstdc++，它依赖 `arc4random@GLIBC_2.36`。Ubuntu 24.04 = glibc 2.39 ✅；Ubuntu 22.04 = glibc 2.35 ❌（本地 dev 机跑不了，必须用容器版或 24.04 runner）。 |
| **runner = ubuntu-24.04** | workflow 显式 `runs-on: ubuntu-24.04` 以满足上面的 glibc 要求。 |
| **docker daemon 可用** | `bazel build` 本身**不需要** docker；但 `bazel run //dockers/docker-orchagent:load`（把 OCI 镜像灌进 dockerd）和后续 `docker save` 需要。runner 自带 docker。 |
| **不需要 JDK** | bazelisk 下载的 bazel 自带 JRE，系统**无需**单独装 `openjdk-*`。容器版 Dockerfile 也未装 JDK，CI 已验证可行。 |

## bazel 本体

| 组件 | 版本 | 安装方式 |
|------|------|----------|
| bazelisk | v1.27.0 | `curl` 下载到 `/usr/bin/bazel`（在所有用户 PATH 上）。运行时自动拉取 **bazel 8.5.1**。 |

## apt 安装的包（`--no-install-recommends`）

| 包 | 用途 |
|----|------|
| `git` | checkout / submodule（actions/checkout 也用） |
| `build-essential` | gcc/g++/make/libc-dev —— repository rule 编译期工具（flex/bison/m4 等源码构建） |
| `pkg-config` | 库探测 |
| `make` | 部分 genrule / repository rule 调用 |
| `patch` | bazel repository rule 打补丁（如 libnl3 的 `bazel_patches/*.patch`） |
| `python3` | bazel genrule / SAI metadata 等脚本 |
| `python3-pip` | 个别 python 工具链需要 |
| `python3-yaml` | 构建脚本解析 yaml |
| `zip` `unzip` | bazel 解包/打包（embedded JDK、归档等） |
| `xz-utils` | 解压 `.tar.xz` 源码包 |
| `clang` | 部分目标用 clang 编译 |
| `llvm-18` `llvm-18-dev` | LLVM 工具/头文件（Ubuntu 24.04 的 LLVM 版本；bookworm 容器版对应 `llvm llvm-dev`=llvm-14） |
| `flex` `bison` | libnl3 等组件的词法/语法分析器（ematch_syntax.y 等） |
| `libicu-dev` | ICU 开发头（unicode） |
| `libncurses-dev` | ncurses 开发头 |
| `libpcre2-dev` | PCRE2 开发头 |
| `libxml2-dev` | libxml2 开发头 |
| `aspell` `aspell-en` | 部分组件构建期的拼写检查（含英文词典 `aspell-en`） |

## 与容器版（`tools/bazel-builder/Dockerfile`）的差异

| 项 | 容器版（bookworm） | host 直跑版（ubuntu-24.04） |
|----|--------------------|------------------------------|
| LLVM | `llvm llvm-dev`（=14） | `llvm-18 llvm-18-dev` |
| docker | `docker-ce-cli`（容器内，挂 socket，无 daemon） | 用 runner 自带的完整 docker |
| `sudo gnupg lsb-release` | 装（容器内提权 + 加 docker apt 源用） | 不需要（runner 已具备） |
| `groupadd/useradd lunyue` | 造匹配用户 | 不需要 |
| JDK | 不装 | 不装 |

## 来源

- 容器版包清单：`tools/bazel-builder/Dockerfile`
- host 安装参考：Bojun-Feng/sonic-bazel-scripts `01_install_bazel.sh`（Ubuntu 24.04 host 安装脚本）
- glibc 要求出处：`tools/bazel-builder/README.md`

## combo workflow 追加的包

`.github/workflows/bazel-orchagent-combo.yml`（host 预构建 bazel 镜像 + `make BAZEL_ORCHAGENT=y
BAZEL_ORCHAGENT_PREBUILT=y target/docker-orchagent.gz`）在上面 host-direct 清单基础上**多装一个**：

| 包 | 用途 |
|----|------|
| `acl` | SONiC make 流程对 slave 容器用 `setfacl`（官方 build-template.yml 也 `sudo apt-get install -y acl`） |

其余依赖与 host-direct 完全一致。combo workflow 还 `sudo modprobe overlay`（SONiC make 的 overlay 存储驱动需要）。
