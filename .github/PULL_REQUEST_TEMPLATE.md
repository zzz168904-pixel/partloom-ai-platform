## 变更

说明修改的公开行为、CAD-IR operation、连接器或 Pipeline 契约。

## 验证

- [ ] 我已阅读 `CONTRIBUTOR_POLICY.md`，并理解外部 PR 在签署单独贡献协议前不会合并
- [ ] `python scripts/release_audit.py` 通过
- [ ] `python -m pytest` 通过
- [ ] 只使用合成数据
- [ ] 未加入客户文件、日志、截图、密钥、个人路径或厂商二进制
- [ ] CAD API 修改仍受 CAD-IR、确认和 Pipeline 门禁控制

## CAD 验收

说明是否需要维护者在隔离的 Windows CAD 环境执行真实 SolidWorks 或
AutoCAD 验收。公共 CI 不应启动有许可证的桌面 CAD 软件。
