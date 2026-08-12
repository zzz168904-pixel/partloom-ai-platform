# PartLoom AI Platform 安装与配置

本文面向希望从 GitHub 源码运行 PartLoom 的 Windows 用户。当前公开的
`v0.1.0-beta.2` Release 只提供 GitHub 自动生成的源码归档，**尚未发布经过签名的
预编译 Windows 安装包**。请不要从非项目官方 Release 页面下载声称是 PartLoom 的
可执行文件。

## 1. 运行边界

PartLoom 从 `v0.1.0-beta.2` 起使用 PolyForm Noncommercial 1.0.0；商业用途需
另行取得书面授权。它连接的软件并不随项目分发：

- SolidWorks、AutoCAD 及其许可证需要用户自行安装和维护；
- 仓库不包含厂商二进制文件、Interop 程序集或模板库；
- MinerU/OCR 模型、云模型 API Key 和客户 CAD 文件不在发行包内；
- 云端 CI 只验证 Python 逻辑，不启动有许可证的 CAD 软件；
- 真实模型必须在本机 CAD 环境中完成保存、关闭和重开验收。

## 2. 系统要求

### 基础环境

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 11 x64 |
| Python | 3.11 至 3.13；开发基线推荐 3.13 |
| 内存 | 最低 16 GB；复杂 CAD 任务推荐 32 GB 或以上 |
| 存储 | SSD，至少预留 10 GB 可用空间 |
| Git | 建议安装 Git for Windows |

### 可选 CAD 组件

| 能力 | 本项目开发/验证基线 | 是否必需 |
|---|---|---|
| SolidWorks 三维建模 | SolidWorks 2025 | 仅运行三维建模时需要 |
| AutoCAD DWG 与标注 | AutoCAD 2025 | 仅运行 DWG/标注流程时需要 |
| PDF 文本解析 | Python 依赖 | PDF2CAD 文本模式需要 |
| 扫描图 OCR | 用户配置 MinerU 或兼容 OCR | 扫描 PDF 时需要 |
| TaskPane Add-in | .NET Framework 4.8、Visual Studio Build Tools、SolidWorks Interop | 实验功能 |

其他 CAD 版本可能兼容，但未列为当前公开验收基线。请在提交 Issue 时说明 Windows、
Python 和 CAD 的精确版本。

## 3. 从源码安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/zzz168904-pixel/partloom-ai-platform.git
cd partloom-ai-platform
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

如果没有 Python 3.13，可将命令中的 `3.13` 替换为 `3.11` 或 `3.12`。

安装完成后先运行不接触 CAD 的检查：

```powershell
.\.venv\Scripts\python.exe scripts\release_audit.py
.\.venv\Scripts\python.exe -m pytest
```

公开基线为 `419 passed, 5 skipped`。跳过项依赖不随仓库公开的真实 CAD 持久引用模型，
不影响 CAD-IR、Pipeline 和连接器逻辑测试。

## 4. 启动桌面 GUI

1. 保存正在编辑的重要 CAD 文档；
2. 使用同一个 Windows 用户启动 SolidWorks；
3. 处理所有 SolidWorks 模态弹窗；
4. 在仓库目录执行：

```powershell
.\.venv\Scripts\python.exe app.py
```

首次验证建议选择 **CAD-IR JSON**，加载：

```text
examples\cad_ir\gui_cad_ir_plate_with_center_hole.json
```

执行前检查计划只包含：

```text
base_plate -> through_hole -> save_sldprt
```

确认后再运行，并检查 SLDPRT、`design_plan.json` 和 `pipeline_report.json`。不要用未保存的
生产文档做首次测试。

## 5. 本地数据与输出

默认路径：

```text
应用数据和日志：%LOCALAPPDATA%\PartLoomAI
任务输出：      %USERPROFILE%\Documents\PartLoom_AI_Output
```

可使用环境变量覆盖：

```powershell
$env:PARTLOOM_APP_DATA = "D:\PartLoom\AppData"
$env:PARTLOOM_OUTPUT_DIR = "D:\PartLoom\Output"
```

每个任务使用独立目录。PartLoom 不应自动删除用户成功模型；失败任务只记录检查点和报告。

## 6. Provider 与密钥

直接 CAD-IR 模式不需要云端 API Key，也是当前最确定的入口。自然语言 Provider 是可选的，
只负责生成候选计划，不能直接调用 Skill、Pipeline 或 CAD API。

复制示例配置：

```powershell
Copy-Item .env.example .env.local
```

然后只在未跟踪的 `.env.local` 或 Windows 用户环境变量中填写自己的 Key。不要把 Key 写入：

- CAD-IR 示例；
- 日志、截图或 Issue；
- Git 提交；
- 构建产物。

如果 Key 曾经公开，必须在 Provider 控制台撤销并重新创建，单纯从文件中删除并不能让旧 Key
失效。

## 7. 启动 localhost Gateway

Gateway 用于 TaskPane 或其他本机客户端共享同一套 Pipeline：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src)
.\.venv\Scripts\python.exe -m cad_agent.gateway `
  --host 127.0.0.1 `
  --port 8765 `
  --token local-development-token
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

受保护接口使用：

```text
Authorization: Bearer local-development-token
```

Gateway 会拒绝绑定非回环地址。它不是可直接暴露到公网的多租户服务。

## 8. 从源码构建 Windows 发行包

维护者可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

脚本会：

1. 创建独立 `.build-venv`；
2. 安装锁定范围内的构建依赖；
3. 运行隐私/密钥/客户资产审计；
4. 运行测试；
5. 使用 PyInstaller 构建目录发行版；
6. 生成 ZIP；
7. 本机安装 Inno Setup 6 时，额外构建安装程序。

跳过安装程序但保留 ZIP：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_release.ps1 -SkipInstaller
```

`-SkipTests` 仅供开发调试，不能用于正式 Release。安装程序在公开发布前还应完成干净虚拟机
测试、恶意软件扫描和代码签名。

## 9. 实验性 SolidWorks TaskPane

源码位于 `src\SolidWorksCadAgentAddin`。它是实验入口，不是 Beta.1 的稳定安装路径。

编译前需要：

- .NET Framework 4.8 Developer Pack；
- Visual Studio 2022 Build Tools 或 Visual Studio；
- 与目标 SolidWorks 版本匹配的 Interop 程序集；
- 设置 `SOLIDWORKS_INTEROP_DIR` 指向程序集目录。

TaskPane 只应调用 localhost Gateway，不应复制 Python Pipeline 或在 C# 中另写一套 CAD
执行逻辑。

## 10. 故障排查

### 无法连接 SolidWorks

- 确认 SolidWorks 已由同一 Windows 用户启动；
- PartLoom 与 SolidWorks 保持相同权限级别；
- 确认许可证有效，并关闭未处理弹窗；
- 不要同时运行多个会写入 ActiveDoc 的 PartLoom 任务；
- 检查 `%LOCALAPPDATA%\PartLoomAI` 中最新日志。

### 提示缺少 pywin32/comtypes

GUI 和 Pipeline 是非交互流程，不会在后台线程询问是否安装依赖。请在 PowerShell 手动执行：

```powershell
.\.venv\Scripts\python.exe -m pip install pywin32 comtypes
```

随后完全退出并重新启动 GUI。正常情况下不应再出现 `lost sys.stdin`。

### SolidWorks 卡顿或内存不足

- 每次只运行一个真实 CAD 任务；
- 关闭不需要的零件、工程图和装配体；
- 先处理 SolidWorks Resource Monitor 警告；
- 避免在生产任务后自动运行批量验收模板；
- 使用阶段化执行，只请求当前需要的模型、工程图或导出；
- 保存工作后重启 SolidWorks，释放长时间 COM 会话积累的内存。

### 计划通过但几何失败

CAD-IR 通过只说明参数、依赖和执行边界合法。真实 CAD 仍可能因为目标 Face/Edge/Body 不唯一、
草图未相交、切除方向错误或版本差异失败。请查看 `pipeline_report.json` 中第一个失败步骤，
不要把只生成文件视为建模成功。

### AutoCAD 不可用

AutoCAD 是可选组件。没有安装并授权 AutoCAD 时，不要请求 `autocad_annotation`。只生成
SolidWorks 零件或直接 CAD-IR 的流程仍可运行。

### PDF 解析返回 unresolved

文本型 PDF 可直接解析；扫描 PDF 需要用户配置 MinerU 或兼容 OCR。尺寸缺失、视图关系不明确
或识别冲突时，PartLoom 会等待确认，不会猜测生产尺寸。

### 文件只读或出现“另存为”窗口

确认目标文件未被其他 SolidWorks 会话、同步软件或网络共享锁定。将任务输出到本地可写目录，
不要覆盖参考模型或客户原文件。

## 11. 卸载与清理

源码安装没有系统级卸载程序。关闭 PartLoom 后可删除仓库与虚拟环境。用户数据不会自动删除；
确认无用后再人工清理：

```text
%LOCALAPPDATA%\PartLoomAI
%USERPROFILE%\Documents\PartLoom_AI_Output
```

如果使用自行构建的 Inno Setup 安装包，请从 Windows“已安装的应用”卸载。无论哪种方式，
卸载都不应删除用户生成的 CAD 文件。
