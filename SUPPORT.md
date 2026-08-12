# 支持范围

## 可以报告

- 安装、启动、发布包或 `release_audit.py` 问题；
- 使用合成 CAD-IR 可复现的校验、路由、Pipeline 或连接器错误；
- [CAPABILITIES.md](CAPABILITIES.md) 已列能力与实际行为不一致；
- [COMPATIBILITY.md](COMPATIBILITY.md) 所列环境中的版本兼容问题。

## 报告前

1. 使用最新受支持的预发布版本重现；
2. 只提交最小合成 CAD-IR、脱敏 traceback 和准确版本号；
3. 删除用户名、绝对路径、客户标识、CAD 文件、日志、截图和密钥；
4. 说明问题属于 CI 逻辑还是需要本机 CAD 验收。

项目不承诺任意零件一次生成成功，也不替代机械设计、制造审核或厂商技术支持。
安全问题必须按 [SECURITY.md](SECURITY.md) 私密报告，不要创建公开 Issue。
