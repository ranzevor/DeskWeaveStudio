# 文档与契约检查

在项目根目录安装检查依赖并执行：

```bash
python -m pip install -r requirements-docs.txt -r requirements-prototypes.txt
python tools/check_repository.py
python tools/check_docs.py --contracts
python tools/check_canonical_json.py
node tools/check_canonical_json.js
python tools/check_authoring_contracts.py
node tools/check_update_signatures.js
```

`check_docs.py` 检查文档链接、导航、需求引用与 JSON 结构；`--contracts` 同时检查 Schema、示例、动作、方法及消息边界。摘要检查核对 Python 和 JavaScript 的规范编码；编辑检查覆盖资源、分页与事务边界；更新检查使用合成签名向量检验信任状态。

原型目录、图像和交互的检查见[原型检查](prototype-validation.md)。应用功能及安装环境的验收场景见[验证与验收设计](verification.md)。

多入口检查覆盖 MCP/CLI 的服务映射、资源 URI 参数、权限、默认值和请求身份结构；目录变体包含合法重排、非法权限变化、默认值漂移、本地方法公开及结果结构替换。表达式预览样例检查 Value AST、合成上下文和结果类型契约，实际求值与状态同步由 Q06/Q37/Q59 覆盖。

MCP 协议检查对照两代适配目录，验证新版元数据、HTTP 头体匹配与编码、订阅确认顺序、范围和结束，以及发现结果与共享目录的一致性。正常示例与直接拒绝示例通过同一检查入口执行。
