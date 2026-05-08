#!/usr/bin/env python3
"""
Heroku XMRig ultra-safe supervisor.
- Reads wallet from config.env, wallet.txt, or Heroku Config Vars.
- Forces low-resource defaults for Heroku.
- Uses RandomX light mode and 1 thread.
- Keeps the Python worker alive even if XMRig is killed by the platform.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.env"
WALLET_FILE = BASE_DIR / "wallet.txt"
TMP_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "xmrig_safe"
XMRIG_VERSION = "6.26.0"
XMRIG_URL = f"https://github.com/xmrig/xmrig/releases/download/v{XMRIG_VERSION}/xmrig-{XMRIG_VERSION}-linux-static-x64.tar.gz"
XMRIG_BIN = TMP_DIR / "xmrig" / "xmrig"

PLACEHOLDERS = {
    "PUT_YOUR_XMR_RECEIVE_ADDRESS_HERE",
    "YOUR_XMR_WALLET",
    "ضع_عنوان_XMR_هنا",
    "عنوان_XMR_الخاص_بك",
    "",
}

stop_requested = False
current_process: Optional[subprocess.Popen] = None


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            parts = shlex.split(value)
            value = parts[0] if parts else ""
        except ValueError:
            value = value.strip('"').strip("'")
        values[key] = value
    return values


def merged_config() -> Dict[str, str]:
    # File values intentionally override Heroku Config Vars when present,
    # so old dashboard vars such as XMR_THREADS=8 cannot break this safe build.
    env = dict(os.environ)
    file_values = parse_env_file(CONFIG_FILE)
    merged = {**env, **file_values}

    wallet = merged.get("XMR_WALLET", "").strip()
    if wallet in PLACEHOLDERS and WALLET_FILE.exists():
        wallet_txt = WALLET_FILE.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
        wallet_txt = [x.strip() for x in wallet_txt if x.strip() and not x.strip().startswith("#")]
        if wallet_txt:
            merged["XMR_WALLET"] = wallet_txt[0]
    return merged


def as_int(value: str, default: int, min_v: int, max_v: int) -> int:
    try:
        num = int(str(value).strip())
    except Exception:
        return default
    return max(min_v, min(max_v, num))


def validate_wallet(wallet: str) -> bool:
    if not wallet or wallet in PLACEHOLDERS:
        return False
    # Monero addresses are usually 95 or 106 chars, but keep this permissive
    # because integrated/subaddresses vary. We only reject obvious bad values.
    return bool(re.fullmatch(r"[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{80,120}", wallet))


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    dest.resolve().mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError(f"Unsafe tar path: {member.name}")
        tar.extractall(dest)


def ensure_xmrig() -> Path:
    if XMRIG_BIN.exists():
        return XMRIG_BIN

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    archive = TMP_DIR / "xmrig.tar.gz"
    extract_dir = TMP_DIR / "extract"
    log(f"⬇️ Downloading XMRig {XMRIG_VERSION}...")
    urllib.request.urlretrieve(XMRIG_URL, archive)
    if extract_dir.exists():
        subprocess.run(["rm", "-rf", str(extract_dir)], check=False)
    extract_dir.mkdir(parents=True, exist_ok=True)
    safe_extract_tar(archive, extract_dir)

    candidates = list(extract_dir.rglob("xmrig"))
    if not candidates:
        raise RuntimeError("XMRig binary not found after extraction")

    target_dir = TMP_DIR / "xmrig"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "xmrig"
    data = candidates[0].read_bytes()
    target.write_bytes(data)
    target.chmod(0o755)
    log(f"✅ XMRig ready: {target}")
    return target


def stream_output(proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        if not line:
            break
        print(line.rstrip("\n"), flush=True)


def start_xmrig(cfg: Dict[str, str], work_seconds: int, sleep_seconds: int) -> subprocess.Popen:
    wallet = cfg["XMR_WALLET"].strip()
    pool = cfg.get("XMR_POOL", "pool.supportxmr.com:3333").strip() or "pool.supportxmr.com:3333"
    worker = cfg.get("WORKER_NAME", "afdaa1").strip() or "afdaa1"
    donate = as_int(cfg.get("DONATE_LEVEL", "1"), 1, 1, 5)

    # Hard safety clamp: Heroku-safe build never allows >1 thread.
    requested_threads = as_int(cfg.get("XMR_THREADS", "1"), 1, 1, 64)
    if requested_threads > 1:
        log(f"⚠️ تم طلب {requested_threads} threads، لكن تم إجبارها إلى 1 لحماية Heroku من status 137.")
    threads = 1

    resource_percent = as_int(cfg.get("RESOURCE_PERCENT", "25"), 25, 1, 25)
    mode = cfg.get("RANDOMX_MODE", "light").strip().lower()
    if mode != "light":
        log("⚠️ تم إجبار RANDOMX_MODE إلى light لتقليل الذاكرة.")
        mode = "light"

    xmrig = ensure_xmrig()
    cmd = [
        str(xmrig),
        "-o", pool,
        "-u", wallet,
        "-p", worker,
        "--coin", "monero",
        "--donate-level", str(donate),
        "--threads", str(threads),
        "--cpu-priority", "0",
        "--cpu-max-threads-hint", str(resource_percent),
        "--randomx-mode", mode,
        "--randomx-wrmsr", "-1",
        "--randomx-no-rdmsr",
        "--randomx-init", "1",
        "--no-huge-pages",
        "--cpu-memory-pool", "0",
        "--print-time", "30",
    ]

    log("✅ تم العثور على عنوان XMR من الملفات/الإعدادات.")
    log(f"🌐 Pool: {pool}")
    log(f"🧩 Worker: {worker}")
    log(f"🛡️ الوضع الآمن: threads={threads}, randomx={mode}, resource≈{resource_percent}%, duty={work_seconds}s عمل / {sleep_seconds}s توقف")
    log("🚀 بدء تشغيل التعدين...")

    def preexec() -> None:
        try:
            os.setsid()
        except Exception:
            pass
        try:
            os.nice(19)
        except Exception:
            pass

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        preexec_fn=preexec,
    )
    threading.Thread(target=stream_output, args=(proc,), daemon=True).start()
    return proc


def stop_child(proc: Optional[subprocess.Popen]) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=8)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
    global stop_requested, current_process
    stop_requested = True
    log(f"🛑 استلام إشارة إيقاف من Heroku: {signum}")
    stop_child(current_process)
    sys.exit(0)


def main() -> None:
    global current_process
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    cfg = merged_config()
    wallet = cfg.get("XMR_WALLET", "").strip()
    if not validate_wallet(wallet):
        log("❌ عنوان XMR غير مضبوط أو غير صحيح.")
        log("ضع العنوان داخل config.env في السطر:")
        log('XMR_WALLET="PUT_YOUR_XMR_RECEIVE_ADDRESS_HERE"')
        log("أو ضع العنوان فقط داخل wallet.txt")
        # Do not crash-loop forever; sleep so logs stay visible.
        while True:
            time.sleep(3600)

    cfg["XMR_WALLET"] = wallet

    resource_percent = as_int(cfg.get("RESOURCE_PERCENT", "25"), 25, 1, 25)
    work_seconds = as_int(cfg.get("DUTY_WORK_SECONDS", "5"), 5, 1, 20)
    sleep_seconds = as_int(cfg.get("DUTY_SLEEP_SECONDS", "15"), 15, 5, 120)
    duty_on = cfg.get("DUTY_CYCLE", "on").lower() not in {"0", "off", "false", "no"}
    auto_backoff = cfg.get("AUTO_BACKOFF", "on").lower() not in {"0", "off", "false", "no"}

    # If user changed resource percent, compute duty cycle if not explicit.
    if "DUTY_WORK_SECONDS" not in cfg and "DUTY_SLEEP_SECONDS" not in cfg:
        work_seconds = 5
        sleep_seconds = max(5, int(work_seconds * (100 - resource_percent) / max(resource_percent, 1)))

    restart_count = 0
    while not stop_requested:
        try:
            current_process = start_xmrig(cfg, work_seconds, sleep_seconds)
        except Exception as exc:
            log(f"❌ فشل تجهيز/تشغيل XMRig: {exc}")
            log("🔁 إعادة المحاولة بعد 60 ثانية...")
            time.sleep(60)
            continue

        while current_process.poll() is None and not stop_requested:
            if not duty_on:
                time.sleep(10)
                continue
            time.sleep(work_seconds)
            if current_process.poll() is not None:
                break
            try:
                os.killpg(os.getpgid(current_process.pid), signal.SIGSTOP)
                log(f"⏸️ تهدئة الموارد لمدة {sleep_seconds} ثانية...")
                time.sleep(sleep_seconds)
                if current_process.poll() is None:
                    os.killpg(os.getpgid(current_process.pid), signal.SIGCONT)
                    log(f"▶️ استئناف التعدين لمدة {work_seconds} ثانية...")
            except ProcessLookupError:
                break
            except Exception as exc:
                log(f"⚠️ تعذر إيقاف/استئناف العملية مؤقتًا: {exc}")
                time.sleep(sleep_seconds)

        code = current_process.poll() if current_process else None
        if stop_requested:
            break

        log(f"⚠️ XMRig خرج بالكود: {code}")
        restart_count += 1

        if auto_backoff and code in {-9, 137}:
            # -9 is SIGKILL; 137 is common container OOM representation.
            sleep_seconds = min(180, max(sleep_seconds * 2, 30))
            work_seconds = max(2, min(work_seconds, 3))
            cfg["RESOURCE_PERCENT"] = str(min(as_int(cfg.get("RESOURCE_PERCENT", "25"), 25, 1, 25), 10))
            log(f"🧯 تم تفعيل تخفيف تلقائي: عمل {work_seconds}s / توقف {sleep_seconds}s / resource<=10%")

        delay = min(300, 20 + restart_count * 10)
        log(f"🔁 إعادة المحاولة بعد {delay} ثانية بوضع آمن...")
        time.sleep(delay)


if __name__ == "__main__":
    main()
