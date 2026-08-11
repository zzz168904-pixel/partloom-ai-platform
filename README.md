# PartLoom AI Platform 项目说明

**版本：0.1.0 Beta.1**

PartLoom 是一款运行于 Windows 的 CAD 自动化工作台，以确定性的 CAD
中间语言（CAD-IR）为核心，连接规划、校验、分阶段执行、SolidWorks、
AutoCAD、PDF2CAD、文件转换和结果报告。语言模型不能绕过门禁直接调用
CAD API。

## 当前能力

- 通过桌面工作台接收并校验 CAD-IR JSON。
- 通过 COM 自动创建和修改已支持的 SolidWorks 零件。
- 按统一 Pipeline 生命周期执行建模特征。
- 在用户明确要求时生成工程图，并导出 SLDPRT、SLDDRW、STEP、STL、
  DWG、DXF 和 PDF。
- 通过 AutoCAD COM 打开并标注兼容的 DWG 文件。
- 将文本型 PDF 或已配置的 MinerU/OCR 结果解析为 Drawing IR。
- 将 DWG、DXF、DWT、SLDPRT、SLDASM、SLDDRW、STEP、IGES、STL、
  X_T 和 X_B 文件路由到对应连接器。
- 记录 Pipeline 报告、校验结果和输出文件位置。

当前 SolidWorks 特征族覆盖常用拉伸、切除、孔、螺纹、阵列、镜像、
圆角、倒角、筋、抽壳、拔模、旋转、扫掠、放样，以及部分钣金、焊件、
齿轮、曲面、装配和工程图操作。支持范围取决于具体 operation 和参数；
Beta 版本不承诺可以生成任意机械几何。

## 完整开源范围

Beta.1 已公开平台核心源码，包括：

- 桌面 GUI、Agent Gateway 与可选 SolidWorks TaskPane Add-in 源码；
- CAD-IR 编译、标准化、参数/单位/依赖校验与直接 JSON 输入；
- Planner Provider、确定性 Validator、Skill Planner 与 Registry；
- Feature Reference Registry、Geometry Resolver 和持久化引用适配器；
- Pipeline 生命周期、阶段门禁、检查点、恢复、报告和保存门禁；
- SolidWorks COM 建模执行器及 AutoCAD、PDF2CAD、File2CAD 连接器；
- 合成 CAD-IR 示例、公开回归测试、打包脚本和安装器源码。

仓库不会包含客户图纸、客户模型、运行日志、生成模型、API Key、厂商
二进制文件或第三方专有模型库。这些是数据与许可证边界，不是隐藏的
平台核心代码。完整说明见 [OPEN_SOURCE_SCOPE.md](OPEN_SOURCE_SCOPE.md)。

## 安全执行边界

```text
用户输入
  -> 候选计划
  -> 标准 CAD-IR
  -> 确定性校验
  -> 用户确认
  -> Pipeline 生命周期
  -> CAD 连接器
  -> 几何与输出校验
  -> 保存门禁和报告
```

不支持的操作、缺失尺寸、有歧义的几何引用和未请求的导出，应在修改
CAD 文档前被阻断。

## 快速开始

### 安装版

1. 运行 `PartLoom-AI-Platform-0.1.0-Beta-Setup.exe`。
2. 启动一次 SolidWorks，确保 COM 注册可用。
3. 从开始菜单启动 **PartLoom AI Platform**。
4. 打开 `examples/cad_ir/gui_cad_ir_plate_with_center_hole.json`。
5. 检查执行计划，确认无误后再开始建模。

AutoCAD 是可选依赖。只有用户明确请求 AutoCAD、DWG 标注或相关导出
阶段时，系统才会启动 AutoCAD。

### 源码版

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe app.py
```

完整环境要求、Provider 配置、构建方法和故障排查见
[INSTALL.md](INSTALL.md)。

## 用户数据位置

默认路径：

- 日志和本地运行状态：`%LOCALAPPDATA%\PartLoomAI`
- 生成文件：`%USERPROFILE%\Documents\PartLoom_AI_Output`

可通过 `PARTLOOM_APP_DATA` 和 `PARTLOOM_OUTPUT_DIR` 修改。

## 模型 Provider

直接 CAD-IR 模式不需要云端 API Key。可选 Provider 适配器包括 OpenAI、
DeepSeek、Claude、Gemini、Qwen 和 Ollama。发布包不包含任何密钥。

源码开发时可将 `.env.example` 复制为本机未跟踪的 `.env.local`，安装版
优先使用 Windows 用户环境变量。

## 公开数据规则

公开仓库只包含合成 CAD-IR 示例。不得提交：

- 客户 PDF、DWG、DXF、SLDPRT、SLDASM、SLDDRW、STEP 或图片；
- 输出模型、截图、报告和日志；
- 微信下载路径或个人绝对路径；
- API Key 或 `.env.local`；
- 厂商二进制文件或专有模型库。

## 版本状态

这是用于受控评估的 Beta.1 版本。制造前必须人工检查全部模型和工程文件。
请阅读 [DISCLAIMER.md](DISCLAIMER.md)。

## 许可证

项目采用 MIT License，详见 [LICENSE](LICENSE)。产品名称和第三方商标
说明见 [NOTICE](NOTICE)。
