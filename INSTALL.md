# PartLoom AI Platform 安装说明

## Beta 版运行要求

桌面应用最低要求：

- Windows 11 64 位
- x64 处理器
- 最低 16 GB 内存；CAD 工作负载推荐 32 GB
- SSD 至少保留 10 GB 可用空间

CAD 连接器要求：

- 当前已验证的三维自动化路径需要 SolidWorks 2025
- AutoCAD/DWG 自动标注流程需要 AutoCAD 2025
- 每个 CAD 软件都需要用户自行准备有效许可证

安装包不包含 SolidWorks、AutoCAD、厂商 Interop 程序集、MinerU 模型
权重或云模型凭据。

## 安装独立版本

1. 关闭 PartLoom 和正在运行的 PartLoom 安装程序。
2. 运行 `PartLoom-AI-Platform-0.1.0-Beta-Setup.exe`。
3. 选择安装目录。默认安装到 `C:`，也支持选择名称清晰的 `D:` 目录。
4. 建议保留开始菜单快捷方式。
5. 第一次执行三维自动化前，先单独启动一次 SolidWorks。
6. 从开始菜单启动 PartLoom。

生成文件不会写入安装目录，默认保存到：

```text
%USERPROFILE%\Documents\PartLoom_AI_Output
```

日志和应用状态默认保存到：

```text
%LOCALAPPDATA%\PartLoomAI
```

## 可选配置

`.env.local` 只建议用于源码开发。安装版优先使用 Windows 用户环境变量。

常用变量：

```text
PARTLOOM_APP_DATA=
PARTLOOM_OUTPUT_DIR=
PARTLOOM_MINERU_HOME=
RAPIDOCR_PYTHON=
SOLIDWORKS_TEMPLATE_DIR=
SOLIDWORKS_INTEROP_DIR=
MINERU_COMMAND=
```

Provider 变量列在 `.env.example` 中。使用直接 CAD-IR 时应保持 API Key
为空。

## 第一次测试

1. 打开 SolidWorks，确认当前没有未保存的客户文档。
2. 启动 PartLoom。
3. 选择 CAD-IR JSON 输入模式。
4. 加载：

   ```text
   examples\cad_ir\gui_cad_ir_plate_with_center_hole.json
   ```

5. 确认预览中只包含用户请求的建模阶段。
6. 执行后检查特征树、尺寸和 SLDPRT，再决定是否接受结果。

## 从源码运行

安装 Python 3.13，然后执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe app.py
```

构建独立程序目录和 ZIP：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

如果已经安装 Inno Setup 6，同一脚本还会生成 Windows 安装程序。

## 可选 SolidWorks TaskPane

TaskPane 源码位于 `src\SolidWorksCadAgentAddin`。该功能在 v0.1.0 Beta 中
仍属于实验能力，需要 SolidWorks Interop 程序集和 .NET Framework 编译器。
自动发现失败时，请先设置 `SOLIDWORKS_INTEROP_DIR`。

## 常见问题

### 无法连接 SolidWorks

- 使用同一个 Windows 用户单独启动一次 SolidWorks。
- 确认 SolidWorks 许可证有效。
- PartLoom 和 SolidWorks 应保持相同权限级别。
- 关闭 SolidWorks 中未处理的弹窗。

### AutoCAD 不可用

AutoCAD 是可选组件。未安装并授权 AutoCAD 时，不要请求 AutoCAD 标注
或相关导出阶段。

### PDF 解析结果显示 unresolved

文本型 PDF 可以不依赖 OCR。扫描图需要配置 MinerU 或 OCR。尺寸缺失或
存在歧义时必须由用户确认，PartLoom 不会猜测生产尺寸。

### API Provider 不可用

直接 CAD-IR 模式不需要密钥。使用云端 Provider 时，请配置新建的用户级
Key 并重启 PartLoom。不要将 Key 粘贴到公开日志、Issue、截图或 CAD-IR
文件中。

## 卸载

在 Windows“已安装的应用”中选择 PartLoom AI Platform 并卸载。卸载程序
不会自动删除用户生成的 CAD 文件；如需清理，请人工检查后再处理
`%LOCALAPPDATA%\PartLoomAI` 和
`%USERPROFILE%\Documents\PartLoom_AI_Output`。
