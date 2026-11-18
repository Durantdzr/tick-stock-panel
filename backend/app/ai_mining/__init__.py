"""LLM 因子进化循环（Phase B M1）。

五步循环: 假设提出 → 因子编码（沙箱执行） → 面板评估 → LLM 反馈 → 记录。
边界声明: 生成代码的静态拦截复用 ai_generator AST 名单（因子合同放行
pandas + parquet IO），真正的隔离是受限子进程（cwd jail + 超时 + 环境清理）；
无网络强制断开（socket 在 AST 层禁用）。信任级别与 ai_generator 生成策略一致。
"""
