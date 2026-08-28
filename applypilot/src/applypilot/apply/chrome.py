"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# CDP (Chrome DevTools Protocol) readiness / health helpers
# ---------------------------------------------------------------------------

def _cdp_version_ok(port: int, timeout: float = 2.0) -> bool:
    """Return True if Chrome's CDP HTTP endpoint answers on `port`."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as resp:
            return getattr(resp, "status", 200) == 200
    except Exception:
        return False


def _wait_for_cdp(port: int, timeout: float = 30.0) -> bool:
    """Poll Chrome's CDP endpoint until it responds or `timeout` elapses.

    Replaces a blind fixed sleep: returns as soon as Chrome is actually ready.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cdp_version_ok(port, timeout=2.0):
            return True
        time.sleep(0.25)
    return False


def is_chrome_healthy(process: subprocess.Popen | None, port: int,
                      timeout: float = 2.0) -> bool:
    """True if the Chrome process is alive AND its CDP endpoint responds.

    Used to decide whether a worker can reuse its existing Chrome or must
    relaunch it.
    """
    if process is None or process.poll() is not None:
        return False
    return _cdp_version_ok(port, timeout=timeout)


def reset_chrome_tabs(port: int, timeout: float = 5.0) -> bool:
    """Best-effort: leave exactly one about:blank tab open between jobs.

    Opens a fresh blank tab first (so closing the rest doesn't quit Chrome),
    then closes the previously-open page tabs. Purely HTTP over CDP — no
    websocket/dependency. Returns True if a blank tab was opened.
    """
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/json", timeout=timeout) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False

    old_page_ids = [
        t.get("id") for t in targets
        if t.get("type") == "page" and t.get("id")
    ]

    # Open a fresh blank tab. Newer Chrome requires PUT; older accepts GET.
    opened = False
    for method in ("PUT", "GET"):
        try:
            req = urllib.request.Request(
                f"{base}/json/new?about:blank", method=method
            )
            with urllib.request.urlopen(req, timeout=timeout):
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False

    for tid in old_page_ids:
        try:
            with urllib.request.urlopen(f"{base}/json/close/{tid}", timeout=timeout):
                pass
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(worker_id: int) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    if (profile_dir / "Default").exists():
        return profile_dir  # Already initialized

    # Find a source: prefer existing worker (has session cookies), else user profile
    source: Path | None = None
    for wid in range(10):
        if wid == worker_id:
            continue
        candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
        if (candidate / "Default").exists():
            source = candidate
            break
    if source is None:
        source = config.get_chrome_user_data()

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    # APPLYPILOT_REAL_PROFILE=1: drive the user's actual Chrome profile
    # (real LinkedIn/Indeed sessions). Requires normal Chrome closed and --workers 1.
    use_real = os.environ.get("APPLYPILOT_REAL_PROFILE", "") == "1"
    if use_real:
        if worker_id != 0:
            raise RuntimeError("APPLYPILOT_REAL_PROFILE requires --workers 1")
        local = os.environ.get("LOCALAPPDATA", "")
        profile_dir = Path(local) / "Google" / "Chrome" / "User Data"
        if not (profile_dir / "Default").exists():
            raise RuntimeError(f"Real Chrome profile not found at {profile_dir}")
        logger.info("[worker-0] Using REAL Chrome profile: %s", profile_dir)
    else:
        profile_dir = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag (skip for real profile)
    if not use_real:
        _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if headless:
        cmd.append("--headless=new")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Wait until Chrome's CDP endpoint is actually up instead of a blind sleep.
    if not _wait_for_cdp(port, timeout=30.0):
        # Fallback: don't hard-break — give Chrome a moment and continue anyway.
        logger.warning(
            "[worker-%d] CDP endpoint not ready after 30s; falling back to fixed wait",
            worker_id,
        )
        time.sleep(3)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None,
                   port: int | None = None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
        port: CDP port to sweep for orphaned Chrome. Defaults to
            BASE_CDP_PORT + worker_id.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    # taskkill /T can miss detached Chrome children; reap anything still holding
    # the CDP port so the next launch has a clean port.
    _kill_on_port(port)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
