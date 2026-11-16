"""KiT runner stdout 错误 stub — stdout 输出 {"ok": false} 结构化错误, exit 1。

对应复审 P2: 编排层应优先解析 stdout 中的结构化错误 (runner 契约), 而非只看 stderr。
"""
import json
import sys

sys.stdin.read()
print(json.dumps({"ok": False, "error_type": "inference_failed", "error": "prepare_inference_window: 具体异常详情 XYZ"}))
sys.exit(1)
