#!/usr/bin/env python3
# test_api.py — simple health / root / chat tester
import os, sys, json, requests

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")

def section(t): print("\n" + "="*8 + " " + t + " " + "="*8)

def check_health():
    section("HEALTH")
    try:
        r = requests.get(f"{BASE}/health", timeout=10)
        print("status:", r.status_code)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except Exception as e:
        print("error:", e)

def check_root():
    section("ROOT /")
    try:
        r = requests.get(f"{BASE}/", timeout=10)
        print("status:", r.status_code, "content-type:", r.headers.get("content-type"))
        print(r.text[:1000])
    except Exception as e:
        print("error:", e)

def test_chat(message):
    section("CHAT")
    payload = {"message": message}
    try:
        r = requests.post(f"{BASE}/chat/", json=payload, timeout=30)
        print("status:", r.status_code)
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        else:
            print(r.text)
    except Exception as e:
        print("error:", e)

def main():
    check_health()
    check_root()
    msg = sys.argv[1] if len(sys.argv) > 1 else "안녕하세요, 레이더 테스트입니다."
    test_chat(msg)

if __name__ == "__main__":
    main()