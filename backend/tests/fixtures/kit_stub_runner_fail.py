"""KiT runner 崩溃 stub — stderr 报错后 exit 3, 无 stdout 结果。"""
import sys

payload = sys.stdin.read()
print("PROGRESS loading_model", file=sys.stderr, flush=True)
print("boom: mock failure detail", file=sys.stderr, flush=True)
sys.exit(3)
