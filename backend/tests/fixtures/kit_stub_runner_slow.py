"""KiT runner 慢速 stub — 挂 30s 不输出, 供超时路径测试。"""
import sys
import time

sys.stdin.read()
time.sleep(30)
