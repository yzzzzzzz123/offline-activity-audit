# 八类PDF规则维护

业务规则：`audit_core/pdf_policy.py`。活跃注册：`scenario_registry.py`。资料读取和AI识别：`pdf_materials.py`。证据和程序复算：`pdf_evidence.py`。LCEL正式链：`pdf_workflow.py`。持久化和UI：`workbench_runtime.py`、`workbench_store.py`。

修改后用 `scripts/sync_pdf_policy.py --write` 同步Skill和JSON，再完整运行测试；不再新增文件名匹配器或按旧十类处理器添加审核。
