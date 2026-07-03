#!/usr/bin/env python3
"""dx-compiler installer — Python port of the legacy install.sh orchestrator.

Behavior-preserving rewrite: the flow, arg surface, OS/arch gating, Python
compatibility handling, dx_com / dx_tron install logic, sample download and
completion messages match install.sh 1:1. The leaf shell scripts under
scripts/ and example/ are still invoked unchanged as subprocesses — only the
orchestration ("business logic") layer moved to Python, plus a rich TUI that
degrades to plain ANSI when rich is unavailable (an installer cannot assume
pip packages exist yet).
"""
from __future__ import annotations

import fnmatch
import os
import platform
import re
import select
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR
VERSION_FILE = PROJECT_ROOT / "compiler.properties"

PROJECT_NAME = "dx-compiler"
MIN_PY_VERSION = "3.8.0"
SUPPORTED_PYTHON_VERSIONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]


# --------------------------------------------------------------------------- #
# UI: rich if available, else ANSI fallback mirroring scripts/color_env.sh.
# All status output goes to stderr, matching install.sh (print_colored >&2).
# --------------------------------------------------------------------------- #
class UI:
    _ANSI = {
        "ERROR": "\033[41m[ERROR]\033[0m\033[91m {} \033[0m",
        "SUCCESS": "\033[42m[SUCCESS]\033[0m\033[92m {} \033[0m",
        "OK": "\033[42m[OK]\033[0m\033[92m {} \033[0m",
        "FAIL": "\033[41m[FAIL]\033[0m\033[91m {} \033[0m",
        "INFO": "\033[44m[INFO]\033[0m\033[94m {} \033[0m",
        "WARNING": "\033[43;30m[WARNING]\033[0m\033[93m {} \033[0m",
        "DEBUG": "\033[43;30m[DEBUG]\033[0m\033[93m {} \033[0m",
        "HINT": "\033[42m[HINT]\033[0m\033[92;40m {} \033[0m",
        "SKIP": "\033[100;37m[SKIP]\033[0m\033[100;37m {} \033[0m",
    }
    _RICH_STYLE = {
        "ERROR": "bold white on red", "FAIL": "bold white on red",
        "SUCCESS": "bold white on green", "OK": "bold white on green",
        "INFO": "bold white on blue", "HINT": "bold black on green",
        "WARNING": "bold black on yellow", "DEBUG": "bold black on yellow",
        "SKIP": "bold white on grey37",
    }

    def __init__(self, debug: bool = False):
        self.debug_enabled = debug
        self.console = None
        rich = _load_rich()
        if rich is not None:
            from rich.console import Console
            self.console = Console(stderr=True, highlight=False)

    def log(self, level: str, message: str) -> None:
        level = level.upper()
        if level == "DEBUG" and not self.debug_enabled:
            return
        if self.console is not None:
            from rich.text import Text
            style = self._RICH_STYLE.get(level)
            t = Text()
            if style:
                t.append(f"[{level}]", style=style)
                t.append(f" {message}")
            else:
                t.append(message)
            self.console.print(t)
        else:
            fmt = self._ANSI.get(level)
            print((fmt.format(message) if fmt else message), file=sys.stderr, flush=True)

    def info(self, m): self.log("INFO", m)
    def error(self, m): self.log("ERROR", m)
    def warning(self, m): self.log("WARNING", m)
    def hint(self, m): self.log("HINT", m)
    def ok(self, m): self.log("OK", m)
    def skip(self, m): self.log("SKIP", m)
    def debug(self, m): self.log("DEBUG", m)
    def success(self, m): self.log("SUCCESS", m)
    def plain(self, m=""): print(m, file=sys.stdout, flush=True)  # bash bare `echo -e` -> stdout

    def header(self, subtitle: str) -> None:
        if self.console is not None:
            from rich.panel import Panel
            self.console.print(Panel.fit(
                f"[bold cyan]DEEPX DX-COMPILER[/]\n[dim]{subtitle}[/]",
                border_style="cyan"))
        else:
            self.plain(f"=== DEEPX DX-COMPILER :: {subtitle} ===")

    def steps(self, items: list[str]) -> None:
        """Render the planned install steps as a checklist panel."""
        if self.console is not None:
            from rich.panel import Panel
            body = "\n".join(f"[cyan]•[/] {s}" for s in items)
            self.console.print(Panel.fit(body, title="Plan", border_style="blue"))
        else:
            self.plain("--- Plan ---")
            for s in items:
                self.plain(f"  - {s}")


def _load_rich():
    """Import rich, best-effort installing it into the user site if missing.

    Never raises: returns the module or None so the UI can fall back to ANSI.
    """
    try:
        import rich  # noqa: F401
        return rich
    except ImportError:
        pass
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "--quiet", "rich"],
            timeout=120, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import site
        usp = site.getusersitepackages()
        if usp and usp not in sys.path:
            sys.path.append(usp)
        import importlib
        importlib.invalidate_caches()
        import rich  # noqa: F401
        return rich
    except Exception:
        return None


ui: UI  # set in main()


# --------------------------------------------------------------------------- #
# Config: replaces install.sh globals.
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    target_pkg: str = "all"
    archive_mode: str = "n"
    docker_volume_path: str = field(default_factory=lambda: os.environ.get("DOCKER_VOLUME_PATH", ""))
    python_version: str = ""
    venv_path_override: str = ""
    venv_symlink_target_path_override: str = ""
    force_remove_venv: bool = True   # matches FORCE_REMOVE_VENV=1 default
    reuse_venv: bool = False
    use_force: bool = True           # matches USE_FORCE=1
    system_site_packages: bool = False
    enable_debug_logs: bool = False
    force_args: str = "--force"      # FORCE_ARGS default "--force"
    use_pypi: bool = True            # USE_PYPI=1

    # resolved at runtime
    venv_path: str = ""
    venv_symlink_target_path: str = ""

    # properties
    com_version: str = ""
    tron_version: str = ""
    tron_download_url: str = ""

    # install status flags
    dx_com_installed: bool = False
    dx_tron_installed: bool = False
    dx_tron_web_only: bool = False

    dry_run: bool = False


class InstallAbort(SystemExit):
    """Raised to mirror install.sh's `popd >&2; exit N`."""


def die(message: str, code: int = 1, with_usage: bool = False):
    # install.sh routed several aborts through `show_help "error" <msg>`, which
    # printed the full usage banner before the [ERROR] line — with_usage mirrors that.
    if with_usage:
        print_usage()
    ui.error(message)
    raise InstallAbort(code)


# --------------------------------------------------------------------------- #
# Properties + environment helpers.
# --------------------------------------------------------------------------- #
def load_properties(cfg: Config) -> None:
    if not VERSION_FILE.is_file():
        die(f"Version file '{VERSION_FILE}' not found.")
    ui.info(f"Loading version properties from '{VERSION_FILE}'...")
    props: dict[str, str] = {}
    for line in VERSION_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip().strip('"').strip("'")
    cfg.com_version = props.get("COM_VERSION", "")
    cfg.tron_version = props.get("TRON_VERSION", "")
    cfg.tron_download_url = props.get("TRON_DOWNLOAD_URL", "")


def read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    p = Path("/etc/os-release")
    if p.is_file():
        for line in p.read_text().splitlines():
            m = re.match(r'^([A-Z_]+)=(.*)$', line.strip())
            if m:
                data[m.group(1)] = m.group(2).strip().strip('"')
    return data


def check_container_mode() -> bool:
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except Exception:
        cgroup = ""
    in_container = bool(re.search(r"/docker|/lxc|/containerd", cgroup)) or Path("/.dockerenv").exists()
    ui.info("(container mode detected)" if in_container else "(host mode detected)")
    return in_container


def _version_sort_max(versions: list[str]) -> str:
    # Emulate `sort -V | tail -1`. Each dotted segment maps to a (rank, value)
    # tuple with consistent types so a non-numeric VERSION_ID (e.g. gLinux
    # "rodete") sorts as a string instead of raising TypeError on int-vs-str.
    def key(v):
        return [(0, int(x)) if x.isdigit() else (1, x) for x in v.split(".")]
    return max(versions, key=key) if versions else ""


def os_check(os_release: dict, os_names: str, ubuntu_v: str, debian_v: str,
             fedora_v: str, rhel_v: str, centos_v: str) -> bool:
    """Port of common_util.sh os_check(): two-pass ID / ID_LIKE detection."""
    ui.info("--- OS Check..... ---")
    # Gate on file existence (like bash `[ ! -f /etc/os-release ]`), not dict
    # emptiness: a present-but-unparseable file must fall through to the
    # "Unsupported operating system" path, not the "file not found" one.
    if not Path("/etc/os-release").is_file():
        ui.error("/etc/os-release file not found. Cannot determine OS information.")
        return False
    os_id = os_release.get("ID", "")
    os_version_id = os_release.get("VERSION_ID", "")
    id_like = os_release.get("ID_LIKE", "")
    ui.info(f"Detected OS: {os_id} {os_version_id}")

    supported = os_names.split()
    detected_os = ""

    # Pass 1: exact ID match.
    for s in supported:
        if os_id == s:
            detected_os = s
            break

    # Explicitly reject unqualified RHEL derivatives (mirrors bash).
    if os_id in ("almalinux", "rocky"):
        return False

    # Pass 2: ID_LIKE fallback in deliberate priority order.
    if not detected_os:
        for s in ("ubuntu", "debian", "rhel", "centos", "fedora"):
            if s not in supported:
                continue
            if re.search(rf"{s}", id_like):
                detected_os = s
                break

    if not detected_os:
        ui.error(f"Unsupported operating system: {os_id}")
        ui.hint(f"Supported operating systems: {os_names} and their compatible distributions")
        return False

    version_map = {
        "ubuntu": ubuntu_v, "debian": debian_v, "fedora": fedora_v,
        "rhel": rhel_v, "centos": centos_v,
    }
    supported_versions = version_map[detected_os].split()
    if detected_os in ("rhel", "centos"):
        major = os_version_id.split(".")[0]
        version_supported = major in supported_versions
    else:
        version_supported = os_version_id in supported_versions

    if not version_supported:
        ui.error(f"Current {detected_os} version {os_version_id} is not officially supported.")
        ui.hint(f"Officially supported {detected_os} versions: {' '.join(supported_versions)}")
        max_v = _version_sort_max(supported_versions)
        newer = bool(os_version_id) and bool(max_v) and \
            _version_sort_max([max_v, os_version_id]) == os_version_id and os_version_id != max_v
        ui.hint(f"Please {'use' if newer else 'upgrade to'} one of the officially "
                f"supported {detected_os} versions listed above.")
        return False

    ui.info(f"{detected_os} {os_version_id} is supported.")
    ui.info("[OK] OS check completed successfully.")
    return True


def arch_check(supported_arch_names: str) -> bool:
    ui.info("--- Arch Check..... ---")
    system_arch = platform.machine()
    if not system_arch:
        ui.error("Failed to determine system architecture using uname -m")
        return False
    ui.info(f"Detected architecture: {system_arch}")
    if system_arch not in supported_arch_names.split():
        ui.error(f"Unsupported architecture: {system_arch}")
        ui.hint(f"Supported architectures: {supported_arch_names}")
        return False
    ui.info(f"Architecture {system_arch} is supported.")
    ui.info("[OK] Architecture check completed successfully.")
    return True


DOC_URL = ("https://github.com/DEEPX-AI/dx-compiler/blob/main/"
           "source/docs/02_01_System_Requirements_of_DX-COM.md")


def os_arch_check(target: str, silent: bool = False) -> bool:
    """Port of install.sh os_arch_check()."""
    os_release = read_os_release()
    if target == "dx_com":
        params = ("ubuntu fedora rhel centos", "20.04 22.04 24.04 26.04", "",
                  "42 43 44 45", "9 10", "9 10")
        arches = "amd64 x86_64"
        os_err = ("This installer supports only Ubuntu 20.04, 22.04, 24.04, 26.04 / "
                  "Fedora 42-45 / RHEL 9-10 / CentOS 9-10.")
        arch_err = "This installer supports only x86_64/amd64 architecture."
    elif target == "dx_tron":
        params = ("ubuntu debian fedora rhel centos", "20.04 22.04 24.04 26.04",
                  "11 12 13", "42 43 44 45", "9 10", "9 10")
        arches = "amd64 x86_64 arm64 aarch64 armv7l"
        os_err = ("This installer supports only Ubuntu 20.04, 22.04, 24.04, 26.04 / "
                  "Debian 11-13 / Fedora 42-45 / RHEL 9-10 / CentOS 9-10.")
        arch_err = ("This installer supports only x86_64/amd64 and "
                    "arm64/aarch64/armv7l architecture.")
    else:
        die(f"{target} is not supported target.")
        return False

    if not os_check(os_release, *params):
        if not silent:
            ui.error(os_err)
            ui.hint(f"For other OS versions, please refer to the manual installation guide at {DOC_URL}")
        return False
    if not arch_check(arches):
        if not silent:
            ui.error(arch_err)
            ui.hint(f"For other architectures, please refer to the manual installation guide at {DOC_URL}")
        return False
    return True


# --------------------------------------------------------------------------- #
# Subprocess helpers.
# --------------------------------------------------------------------------- #
def venv_env(cfg: Config) -> dict:
    """Environment with cfg.venv_path activated (mirrors `source venv/activate`)."""
    env = os.environ.copy()
    if cfg.venv_path:
        env["VIRTUAL_ENV"] = cfg.venv_path
        env["PATH"] = f"{cfg.venv_path}/bin:" + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
    return env


def venv_python(cfg: Config) -> str:
    if cfg.venv_path:
        cand = Path(cfg.venv_path) / "bin" / "python"
        if cand.exists():
            return str(cand)
    return sys.executable


def run(cmd, *, env=None, cwd=None, label=None, capture=False):
    """Run a command; on non-zero exit, die() (mirrors bash `|| { ...; exit 1; }`)."""
    if label:
        ui.plain(f"CMD: {label}")
    try:
        if capture:
            proc = subprocess.run(cmd, env=env, cwd=cwd or PROJECT_ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.stdout:
                print(proc.stdout, file=sys.stderr, end="")
            return proc.returncode, proc.stdout
        proc = subprocess.run(cmd, env=env, cwd=cwd or PROJECT_ROOT)
        return proc.returncode, ""
    except FileNotFoundError as e:
        die(f"Command not found: {e}")


def sudo_cmd() -> list[str]:
    """Mirror common_util.sh: use `sudo` when present, else run directly (its
    `sudo(){ "$@"; }` shim for root / minimal containers where sudo is absent)."""
    return ["sudo"] if shutil.which("sudo") else []


def run_stream_capture(cmd, *, env=None, cwd=None):
    """Run cmd, stream merged stdout+stderr live (like `... 2>&1 | tee`) while
    capturing the full text. Returns (returncode, captured_output)."""
    proc = subprocess.Popen(cmd, env=env, cwd=cwd or PROJECT_ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stderr.write(line)
        sys.stderr.flush()
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)


def timed_input(prompt: str, timeout: int = 10):
    """`read -t <timeout>`: returns entered text (may be empty) or None on timeout."""
    ui.warning(prompt)
    sys.stderr.flush()
    try:
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        rlist = []
    if rlist:
        raw = sys.stdin.readline()
        # readline() == "" is EOF (closed/`/dev/null` stdin) — bash `read` returns
        # failure there, so treat it as a timeout (None), not a bare-Enter ("").
        if raw == "":
            return None
        return raw.rstrip("\n")
    return None


# --------------------------------------------------------------------------- #
# Install phases (ported from install.sh functions).
# --------------------------------------------------------------------------- #
def validate_environment(cfg: Config) -> None:
    ui.plain("=== validate_environment() [START] ===")
    if cfg.force_remove_venv and cfg.reuse_venv:
        die("Cannot use both --venv-force-remove and --venv-reuse simultaneously. "
            "Please choose one.", with_usage=True)
    if not cfg.com_version:
        die(f"COM_VERSION not defined in '{VERSION_FILE}'.")
    if not cfg.tron_version or not cfg.tron_download_url:
        die(f"TRON_VERSION or TRON_DOWNLOAD_URL not defined in '{VERSION_FILE}'.")
    ui.plain("=== validate_environment() [DONE] ===")


def install_os_default_python3(cfg: Config) -> None:
    osr = read_os_release()
    os_id = osr.get("ID", "")
    os_version_id = osr.get("VERSION_ID", "")
    id_like = osr.get("ID_LIKE", "")
    ui.info("Installing the OS default Python 3...")

    if os_id not in ("ubuntu", "debian", "fedora", "rhel", "centos"):
        if re.search(r"(rhel|fedora|centos)", id_like):
            os_id = "rhel"
        elif re.search(r"(debian|ubuntu)", id_like):
            os_id = "debian"

    if cfg.dry_run:
        ui.skip(f"[dry-run] would install OS default python3 for '{os_id}'")
        return

    sudo = sudo_cmd()
    if os_id in ("ubuntu", "debian"):
        run([*sudo, "apt-get", "update"])
        run([*sudo, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "tzdata"])
        run([*sudo, "apt-get", "install", "-y", "python3", "python3-venv", "python3-dev"])
    elif os_id in ("fedora", "rhel", "centos"):
        default_fedora_py = ""
        if os_id == "fedora" and shutil.which("dnf"):
            # Command substitution in bash swallows a missing/failed dnf -> "";
            # do the same here instead of aborting via run().
            try:
                out = subprocess.check_output(
                    ["dnf", "repoquery", "--quiet", "--qf", "%{version}", "python3"],
                    text=True, stderr=subprocess.DEVNULL)
            except Exception:
                out = ""
            cands = [l for l in out.splitlines() if re.match(r"^\d+\.\d+", l)]
            if cands:
                default_fedora_py = ".".join(sorted(cands, key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])[-1].split(".")[:2])
        is_default_supported = (default_fedora_py in SUPPORTED_PYTHON_VERSIONS) if default_fedora_py else True
        if not is_default_supported:
            latest = SUPPORTED_PYTHON_VERSIONS[-1]
            ui.info(f"Fedora {os_version_id or ''} OS Default python3 version is "
                    f"{default_fedora_py} which is not supported. Installing python{latest}.")
            cfg.python_version = latest
            ui.plain(f"=== install_os_default_python3() redirected to python{latest} ===")
            return
        run([*sudo, "dnf", "install", "-y", "python3", "python3-devel"])
    else:
        die(f"Unsupported OS '{os_id}' for automatic Python installation. "
            "Please install Python 3 manually.")

    if not _which("python3"):
        die("Failed to install the OS default Python 3. Exiting.")
    installed = _detect_py_version("python3")
    ui.info(f"[OK] Installed OS default Python {installed}.")


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def _detect_py_version(python_bin: str) -> str | None:
    try:
        out = subprocess.check_output(
            [python_bin, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            text=True, stderr=subprocess.DEVNULL).strip()
        return out or None
    except Exception:
        return None


def check_python_version_compatibility(cfg: Config) -> None:
    ui.plain("=== check_python_version_compatibility() [START] ===")
    if check_container_mode() and not cfg.docker_volume_path:
        die("--docker_volume_path must be provided in container mode.", with_usage=True)

    current = _detect_py_version("python3")
    supported_str = ", ".join(SUPPORTED_PYTHON_VERSIONS)

    if not current:
        ui.plain()
        for line in ("====================================================================",
                     "  Python 3 is not installed or could not be detected.",
                     f"  Supported Python versions: {supported_str}",
                     "===================================================================="):
            ui.warning(line)
        ui.plain()
        if cfg.python_version:
            ui.info(f"Installing user-specified Python {cfg.python_version} (interactive prompts skipped).")
            ui.plain("=== check_python_version_compatibility() [DONE] ===")
            return

        resp = timed_input("Do you want to install Python 3? (y/n)\n"
                           "(Will proceed with installation on the OS default Python 3 "
                           "in 10 seconds if no response)")
        if resp is not None:
            if resp and not re.match(r"^[Yy]$", resp):
                die("Installation aborted by user.")
            ui.plain()
            chosen = timed_input(
                "Please enter the Python version you want to install (e.g., 3.11, 3.12):\n"
                f"Supported versions: {supported_str}\n"
                "(Will proceed with installation on the OS default Python 3 in 10 seconds if no response)")
            if chosen:
                if chosen not in SUPPORTED_PYTHON_VERSIONS:
                    ui.plain()
                    for line in ("====================================================================",
                                 f"  Unsupported Python version entered: {chosen}",
                                 f"  Supported Python versions: {supported_str}",
                                 "  Aborting installation.",
                                 "===================================================================="):
                        ui.error(line)
                    ui.plain()
                    raise InstallAbort(1)
                cfg.python_version = chosen
                ui.info(f"Will install user-selected Python {cfg.python_version}.")
                ui.plain("=== check_python_version_compatibility() [DONE] ===")
                return
            ui.plain()
            ui.info("No version entered. Proceeding with the OS default Python 3.")
        else:
            ui.plain()
            ui.info("No response received within 10 seconds. Proceeding with the OS default Python 3.")

        install_os_default_python3(cfg)
        ui.plain("=== check_python_version_compatibility() [DONE] ===")
        return

    ui.info(f"Detected Python version: {current}")
    if current in SUPPORTED_PYTHON_VERSIONS:
        ui.info(f"Python version {current} is compatible. Proceeding...")
        ui.plain("=== check_python_version_compatibility() [DONE] ===")
        return

    # Not in supported list: enforce lower bound MIN_PY_VERSION.
    min_major, min_minor = (int(x) for x in MIN_PY_VERSION.split(".")[:2])
    cur_major, cur_minor = (int(x) for x in current.split(".")[:2])
    if cur_major < min_major or (cur_major == min_major and cur_minor < min_minor):
        ui.plain()
        for line in ("====================================================================",
                     "  Python version compatibility check failed!",
                     f"  Detected Python version: {current}",
                     f"  Minimum required Python version: {MIN_PY_VERSION}",
                     f"  Supported Python versions: {supported_str}",
                     "  Aborting: the detected Python version is too old to continue.",
                     "===================================================================="):
            ui.error(line)
        ui.plain()
        raise InstallAbort(1)

    # At/above minimum but newer than the supported list.
    if cfg.python_version:
        ui.plain()
        for line in ("====================================================================",
                     "  Detected Python version is newer than the supported list.",
                     f"  Detected Python version: {current}",
                     f"  Supported Python versions: {supported_str}",
                     f"  Proceeding with user-specified Python {cfg.python_version}.",
                     "===================================================================="):
            ui.warning(line)
        ui.plain()
    else:
        latest = SUPPORTED_PYTHON_VERSIONS[-1]
        ui.plain()
        for line in ("====================================================================",
                     "  Detected Python version is newer than the supported list.",
                     f"  Detected Python version: {current}",
                     f"  Supported Python versions: {supported_str}",
                     f"  Redirecting to Python {latest} (latest supported).",
                     "===================================================================="):
            ui.warning(line)
        ui.plain()
        cfg.python_version = latest
    ui.plain("=== check_python_version_compatibility() [DONE] ===")


def install_python_and_venv(cfg: Config) -> None:
    ui.info("--- Install Python and Create Virtual environment..... ---")
    container = check_container_mode()
    if container:
        if not cfg.docker_volume_path:
            die("--docker_volume_path must be provided in container mode.", with_usage=True)
        cfg.venv_symlink_target_path = f"{cfg.docker_volume_path}/venv/{PROJECT_NAME}"
        cfg.venv_path = f"{PROJECT_ROOT}/venv-{PROJECT_NAME}"
    else:
        cfg.venv_path = f"{PROJECT_ROOT}/venv-{PROJECT_NAME}-local"
        cfg.venv_symlink_target_path = ""

    if cfg.venv_path_override:
        cfg.venv_path = cfg.venv_path_override
        ui.info(f"Using user-specified VENV_PATH: {cfg.venv_path}")
    else:
        ui.info(f"Auto-detected VENV_PATH: {cfg.venv_path}")

    if cfg.venv_symlink_target_path_override:
        cfg.venv_symlink_target_path = cfg.venv_symlink_target_path_override
        ui.info(f"Using user-specified VENV_SYMLINK_TARGET_PATH: {cfg.venv_symlink_target_path}")
    elif cfg.venv_symlink_target_path:
        ui.info(f"Auto-detected VENV_SYMLINK_TARGET_PATH: {cfg.venv_symlink_target_path}")

    args = [str(PROJECT_ROOT / "scripts" / "install_python_and_venv.sh")]
    if cfg.python_version:
        args.append(f"--python_version={cfg.python_version}")
    if MIN_PY_VERSION:
        args.append(f"--min_py_version={MIN_PY_VERSION}")
    if cfg.venv_path:
        args.append(f"--venv_path={cfg.venv_path}")
    if cfg.venv_symlink_target_path:
        args.append(f"--symlink_target_path={cfg.venv_symlink_target_path}")
    if cfg.use_force or cfg.force_remove_venv:
        args.append("--venv-force-remove")
    if cfg.reuse_venv:
        args.append("--venv-reuse")
    if cfg.system_site_packages:
        args.append("--system-site-packages")

    if cfg.dry_run:
        ui.skip(f"[dry-run] would run: {' '.join(args)}")
        return
    rc, _ = run(args, label=" ".join(args))
    if rc != 0:
        die("Failed to Install Python and Create Virtual environment. Exiting.")
    ui.ok("Completed to Install Python and Create Virtual environment.")


def install_pip_packages(cfg: Config) -> None:
    ui.info("Checking for required Python packages (requests, beautifulsoup4)...")
    for pkg in ("requests", "bs4"):
        _install_python_package(cfg, pkg)
    ui.info("All required Python packages are installed.")


def _install_python_package(cfg: Config, package_name: str) -> None:
    py = venv_python(cfg)
    if cfg.dry_run:
        ui.skip(f"[dry-run] would ensure python package '{package_name}'")
        return
    if subprocess.run([py, "-c", f"import {package_name}"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        ui.info(f"Python package '{package_name}' is already installed.")
        return
    ui.info(f"Python package '{package_name}' not found. Installing...")
    if subprocess.run([py, "-m", "pip", "install", package_name], env=venv_env(cfg)).returncode != 0:
        die(f"ERROR: Failed to install Python package '{package_name}'. "
            "Please ensure pip3 is installed and accessible, or install it manually.")
    ui.info(f"Python package '{package_name}' installed successfully.")


def setup_project(cfg: Config) -> None:
    ui.plain(f"=== setup_{PROJECT_NAME}() [START] ===")
    if os.environ.get("VIRTUAL_ENV"):
        install_pip_packages(cfg)
    elif cfg.dry_run or Path(cfg.venv_path).is_dir():
        install_pip_packages(cfg)
    else:
        die(f"Virtual environment '{cfg.venv_path}' is not exist.")
    ui.plain(f"=== setup_{PROJECT_NAME}() [DONE] ===")


def install_prerequisites(cfg: Config) -> None:
    ui.info("--- Install Prerequisites..... ---")
    cmd = [str(PROJECT_ROOT / "scripts" / "install_prerequisites.sh")]
    ui.plain(f"CMD: {cmd[0]}")
    if cfg.dry_run:
        ui.skip("[dry-run] would run install_prerequisites.sh")
        return
    rc, _ = run(cmd, env=venv_env(cfg))
    if rc != 0:
        die("Failed to Install Prerequisites. Exiting.")
    ui.info("[OK] Completed to Install Prerequisites.")


def install_dx_com(cfg: Config) -> None:
    ui.plain("=== install_dx_com() [START] ===")
    dx_as_path = os.path.realpath(str(PROJECT_ROOT / ".."))

    if cfg.python_version:
        py_tag = "cp" + cfg.python_version.replace(".", "")
    else:
        py_tag = _detect_py_tag(cfg)
        if not py_tag:
            die("Failed to detect Python version.")
    if cfg.python_version:
        ui.info(f"Using user-specified Python version tag: {py_tag}")
    else:
        ui.info(f"Detected Python version tag: {py_tag}")

    archive_output_file = os.environ.get("ARCHIVE_OUTPUT_FILE", "")

    if cfg.archive_mode == "y":
        archive_dir = f"{dx_as_path}/archives"
        os.makedirs(archive_dir, exist_ok=True)
        ui.info(f"ARCHIVE_MODE is ON. Downloading dx-com to {archive_dir}...")
        pip_dl = ["--dest", archive_dir, "--no-deps", "--only-binary=:all:",
                  "--python-version", py_tag[2:]]
        py = venv_python(cfg)
        if cfg.dry_run:
            ui.skip("[dry-run] would pip download dx-com for archiving")
            cfg.dx_com_installed = True
            ui.plain("=== install_dx_com() [DONE] ===")
            return
        if cfg.use_pypi:
            if subprocess.run([py, "-m", "pip", "download", *pip_dl, "dx-com"], env=venv_env(cfg)).returncode != 0:
                die("Failed to download dx-com for archiving.")
            archived = _find_wheel(archive_dir, rf"dx_com-.*-{re.escape(py_tag)}-.*\.whl")
        else:
            find_links = f"https://sdk.deepx.ai/release/dxcom/v{cfg.com_version}/index.html"
            if subprocess.run([py, "-m", "pip", "download", *pip_dl,
                               f"dx-com=={cfg.com_version}", "--no-index", "-f", find_links],
                              env=venv_env(cfg)).returncode != 0:
                die("Failed to download dx-com for archiving.")
            archived = _find_wheel(archive_dir, rf"dx_com-{re.escape(cfg.com_version)}-{re.escape(py_tag)}-.*\.whl")
        if archived and archive_output_file:
            with open(archive_output_file, "a") as f:
                f.write(f"ARCHIVED_COM_FILE={archived}\n")
        ui.info(f"dx-com archived: {archived}")
        if not archived:
            ui.warning(f"Warning: Downloaded wheel not found in {archive_dir}. Archive registration skipped.")
        ui.plain("=== install_dx_com() [DONE] ===")
        cfg.dx_com_installed = True
        return

    py = venv_python(cfg)
    if py_tag == "cp38" and not cfg.dry_run:
        ui.info("Python 3.8 detected: Upgrading pip and installing onnxruntime 1.18.0 from direct URL...")
        if subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], env=venv_env(cfg)).returncode != 0:
            ui.warning("Warning: Failed to upgrade pip. Continuing...")
        ort_url = ("https://files.pythonhosted.org/packages/1b/74/"
                   "02cb1f6fcbadc094c98c49aff8571e7c576bdb4015c01507c385285b5bed/"
                   "onnxruntime-1.18.0-cp38-cp38-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl")
        if subprocess.run([py, "-m", "pip", "install", ort_url], env=venv_env(cfg)).returncode == 0:
            ui.info("onnxruntime 1.18.0 installed successfully for Python 3.8!")
        else:
            die("Failed to install onnxruntime 1.18.0 for Python 3.8.")

    if cfg.use_pypi:
        ui.info("Installing dx-com (latest from PyPI)...")
    else:
        ui.info(f"Installing dx-com (Version: {cfg.com_version})...")

    if cfg.dry_run:
        ui.skip("[dry-run] would pip install dx-com")
        cfg.dx_com_installed = True
        ui.plain("=== install_dx_com() [DONE] ===")
        return

    if cfg.force_args:
        ui.info("Force mode: uninstalling existing dx-com before reinstall...")
        subprocess.run([py, "-m", "pip", "uninstall", "-y", "dx-com"],
                       env=venv_env(cfg), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if cfg.use_pypi:
        ui.info("Installing dx-com from PyPI...")
        if subprocess.run([py, "-m", "pip", "install", "dx-com"], env=venv_env(cfg)).returncode == 0:
            ui.info("dx-com installed successfully from PyPI!")
        else:
            die("Failed to install dx-com from PyPI.")
    else:
        find_links = f"https://sdk.deepx.ai/release/dxcom/v{cfg.com_version}/index.html"
        ui.info(f"Installing dx-com from {find_links}...")
        if subprocess.run([py, "-m", "pip", "install", f"dx-com=={cfg.com_version}", "-f", find_links],
                          env=venv_env(cfg)).returncode == 0:
            ui.info("dx-com installed successfully!")
        else:
            die("Failed to install dx-com.")

    ui.plain("=== install_dx_com() [DONE] ===")
    cfg.dx_com_installed = True


def _detect_py_tag(cfg: Config) -> str | None:
    v = _detect_py_version(venv_python(cfg))
    return ("cp" + v.replace(".", "")) if v else None


def _find_wheel(directory: str, pattern: str) -> str:
    # bash used `find "$dir" -name ... -type f` (recursive, files only). Match
    # that with rglob + is_file; sorted gives a deterministic tie-break.
    rx = re.compile(pattern)
    for p in sorted(Path(directory).rglob("*.whl")):
        if p.is_file() and rx.fullmatch(p.name):
            return str(p)
    return ""


def install_dx_tron(cfg: Config) -> None:
    ui.plain("=== install_dx_tron() [START] ===")
    archive_output_file = os.environ.get("ARCHIVE_OUTPUT_FILE", "")

    cmd = [str(PROJECT_ROOT / "scripts" / "install_module.sh"),
           "--module_name=dx_tron", f"--version={cfg.tron_version}",
           f"--download_url={cfg.tron_download_url}"]
    if cfg.archive_mode == "y":
        ui.info("ARCHIVE_MODE is ON.")
        cmd.append("--archive_mode=y")
    if cfg.force_args:
        cmd.append(cfg.force_args)
    if cfg.enable_debug_logs:
        cmd.append("--verbose")

    ui.info(f"Installing dx-tron (Version: {cfg.tron_version})...")
    ui.debug(f"Executing: {' '.join(cmd)}")

    if cfg.dry_run:
        ui.skip(f"[dry-run] would run: {' '.join(cmd)}")
        cfg.dx_tron_installed = True
        ui.plain("=== install_dx_tron() [DONE] ===")
        return

    rc, out = run_stream_capture(cmd, env=venv_env(cfg))  # tee-like: live output + capture
    if rc != 0:
        die("Installing dx-tron failed!")

    if cfg.archive_mode == "y":
        archived = ""
        for line in out.splitlines():
            if line.startswith("ARCHIVED_FILE_PATH="):
                archived = line.split("=", 1)[1]
        if archived and archive_output_file:
            with open(archive_output_file, "a") as f:
                f.write(f"ARCHIVED_TRON_FILE={archived}\n")

    if cfg.archive_mode != "y":
        dx_tron_dir = PROJECT_ROOT / "dx_tron"
        osr = read_os_release()
        install_os_id = osr.get("ID", "")
        id_like = osr.get("ID_LIKE", "")
        os_family = "debian"
        if install_os_id in ("fedora", "rhel", "centos"):
            os_family = "redhat"
        elif re.search(r"(fedora|rhel|centos)", id_like):
            os_family = "redhat"

        if os_family == "redhat":
            ui.info("INFO: Red Hat family detected - installing dx_tron web variant only.")
            ui.info("INFO: (dxtron CLI/desktop AppImage is supported only on Debian/Ubuntu family.)")
            web_dir = _find_first(dx_tron_dir, "*_web") or _find_first(dx_tron_dir, "*_web*")
            if not web_dir:
                die(f"ERROR: dx_tron web variant not found under '{dx_tron_dir}'.")
            ui.info(f"INFO: Found dx_tron web variant: {Path(web_dir).name}")
            cfg.dx_tron_web_only = True
        else:
            arch = platform.machine()
            arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf"}.get(arch, arch)
            deb = _find_first(dx_tron_dir, f"*_{arch}.deb") or _find_first(dx_tron_dir, "*.deb")
            if deb and Path(deb).is_file():
                ui.info(f"INFO: Found DEB package: {Path(deb).name}")
                ui.info("INFO: Installing DX-Tron DEB package...")
                ok = (run(["sudo", "apt-get", "update"])[0] == 0 and
                      run(["sudo", "apt-get", "install", "-y", deb])[0] == 0)
                if ok:
                    ui.info("INFO: DX-Tron DEB package installed successfully!")
                else:
                    die(f"ERROR: Failed to install DX-Tron DEB package '{Path(deb).name}'.")
            else:
                die(f"ERROR: No DEB package found in '{dx_tron_dir}'.")

    ui.plain("=== install_dx_tron() [DONE] ===")
    cfg.dx_tron_installed = True


def _find_first(root: Path, glob_pattern: str) -> str:
    # bash used `find -L` (follows symlinks). pathlib rglob does NOT descend
    # symlinked dirs, so walk with followlinks=True; sorted for determinism.
    # ponytail: no symlink-cycle guard — a dx_tron tarball layout is acyclic.
    if not root.exists():
        return ""
    matches = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        for name in dirnames + filenames:
            if fnmatch.fnmatch(name, glob_pattern):
                matches.append(os.path.join(dirpath, name))
    return sorted(matches)[0] if matches else ""


def download_sample_data(cfg: Config) -> None:
    ui.plain()
    ui.plain("=== download_sample_data() [START] ===")
    ui.info("Running sample data download steps...")
    example_dir = PROJECT_ROOT / "example"

    steps = [
        ("[1/2] Downloading sample models...", "1-download_sample_models.sh", "Sample model download failed."),
        ("[2/2] Downloading sample calibration dataset...", "2-download_sample_calibration_dataset.sh",
         "Calibration dataset download failed."),
    ]
    for msg, script, warn in steps:
        ui.plain()
        ui.info(msg)
        target = example_dir / script
        if cfg.dry_run:
            ui.skip(f"[dry-run] would run: {target}")
            continue
        rc, _ = run([str(target)])
        if rc != 0:
            ui.warning(f"{warn} You can run it manually:")
            ui.hint(f"  {target}")
    ui.plain()
    ui.plain("=== download_sample_data() [DONE] ===")


def show_installation_complete_message(cfg: Config) -> None:
    if cfg.archive_mode == "y":
        return
    if cfg.dx_com_installed and cfg.dx_tron_installed:
        modules = "dx_com and dx_tron"
    elif cfg.dx_com_installed:
        modules = "dx_com"
    elif cfg.dx_tron_installed:
        modules = "dx_tron"
    else:
        return

    ui.plain()
    ui.hint("====================================================================")
    ui.hint(f"  {modules} installation completed!")
    ui.hint("")
    if cfg.dx_com_installed:
        ui.hint("  To use dx_com, activate the virtual environment first:")
        ui.hint(f"    $ source {cfg.venv_path}/bin/activate")
        ui.hint("")
        ui.hint("  Then you can run dxcom:")
        ui.hint("    $ dxcom -h")
        ui.hint("")
    if cfg.dx_tron_installed:
        if cfg.dx_tron_web_only:
            ui.hint("  dxtron (CLI/desktop) is supported only on Debian/Ubuntu family.")
            ui.hint("  On Red Hat family (Fedora/RHEL/CentOS), only the web variant is installed.")
            ui.hint("")
            ui.hint("  To start the dxtron web server:")
            ui.hint("    $ ./run_dxtron_web.sh --port=8080")
            ui.hint("")
        else:
            ui.hint("  To run dxtron (no virtual environment required):")
            ui.hint("    $ dxtron")
            ui.hint("")
            ui.hint("  Or use the convenience script to start the web server:")
            ui.hint("    $ ./run_dxtron_web.sh --port=8080")
            ui.hint("")
            ui.hint("  Note: the 'dxtron' CLI/desktop binary is supported only on Debian/Ubuntu family.")
            ui.hint("")
    ui.hint("====================================================================")
    ui.plain()


# --------------------------------------------------------------------------- #
# main() dispatch — mirrors install.sh main().
# --------------------------------------------------------------------------- #
def run_main(cfg: Config) -> None:
    if cfg.target_pkg == "dx_com":
        ui.info("Installing dx-com...")
        ui.steps(["OS/Arch check (dx_com)", "Validate env", "Python compat",
                  "Python + venv", "pip deps", "Prerequisites", "dx_com", "Sample data"])
        if not os_arch_check("dx_com"):
            raise InstallAbort(1)
        validate_environment(cfg)
        check_python_version_compatibility(cfg)
        install_python_and_venv(cfg)
        setup_project(cfg)
        install_prerequisites(cfg)
        install_dx_com(cfg)
        download_sample_data(cfg)
        ui.info("[OK] Installing dx-com completed successfully.")
        show_installation_complete_message(cfg)

    elif cfg.target_pkg == "dx_tron":
        ui.info("Installing dx-tron...")
        ui.steps(["OS/Arch check (dx_tron)", "Validate env", "Python + venv",
                  "pip deps", "dx_tron"])
        if not os_arch_check("dx_tron"):
            raise InstallAbort(1)
        validate_environment(cfg)
        install_python_and_venv(cfg)
        setup_project(cfg)
        install_dx_tron(cfg)
        ui.info("[OK] Installing dx-tron completed successfully.")
        show_installation_complete_message(cfg)

    elif cfg.target_pkg == "all":
        ui.info("Installing all compiler modules...")
        validate_environment(cfg)
        if cfg.archive_mode == "y":
            will_com, will_tron = True, True
        else:
            will_com = os_arch_check("dx_com", silent=True)
            will_tron = os_arch_check("dx_tron", silent=True)
        if not will_com and not will_tron:
            die("Neither dx-com nor dx-tron is supported on this OS/Architecture. Nothing to install.")
        ui.steps(["Validate env",
                  f"dx_com: {'yes' if will_com else 'skip'}",
                  f"dx_tron: {'yes' if will_tron else 'skip'}",
                  "Python + venv", "pip deps", "Sample data (if dx_com)"])
        if will_com:
            check_python_version_compatibility(cfg)
        install_python_and_venv(cfg)
        setup_project(cfg)
        if will_tron:
            install_dx_tron(cfg)
        else:
            ui.skip("dx-tron is not supported on this OS/Architecture. Skipping dx-tron installation.")
        if will_com:
            install_prerequisites(cfg)
            install_dx_com(cfg)
            if cfg.dx_com_installed:
                download_sample_data(cfg)
        else:
            ui.skip("dx-com is not supported on this OS/Architecture. Skipping dx-com installation.")
        ui.info("[OK] Installing all compiler modules completed successfully.")
        show_installation_complete_message(cfg)
    else:
        die(f"Invalid target '{cfg.target_pkg}'. Valid targets are: dx_com, dx_tron, all",
            with_usage=True)


# --------------------------------------------------------------------------- #
# Argument parsing — hand parser mirroring install.sh's `while/case "$1"` loop
# 1:1: equals-form values only, exact bare flags, in-order processing (so -f/-r
# override each other by position), unknown / `-h` / bare-value tokens abort with
# exit 1 + the usage banner, `--help` prints usage and exits 0. This deliberately
# does NOT use argparse, whose prefix-abbreviation, space-form values, and exit
# code 2 diverge from the shell (see the port-fidelity audit).
# --------------------------------------------------------------------------- #
USAGE = """Usage: install.py [OPTIONS]

Options:
  [--target=<module_name>]              Install specific module (dx_com | dx_tron | all) (default: all)
  [--archive_mode=<y|n>]                Set archive mode (default: n).

  [--docker_volume_path=<path>]         Set Docker volume path (required in container mode)
  [--python_version=<version>]          Specify Python version to install (e.g., 3.11, 3.12)

  [--verbose]                           Enable verbose (debug) logging.
  [--force=<true|false>]                Force reinstall modules (dx_com, dx_tron) even if already installed (default: true)
  [--pypi=<true|false>]                 Install dx-com from PyPI (true) or the DEEPX release index (false) (default: true)
  [--help]                              Display this help message and exit.

Virtual Environment Options:
  [--venv_path=<path>]                  Set virtual environment path (default: PROJECT_ROOT/venv-dx-compiler-local)
  [--venv_symlink_target_path=<dir>]    Set symlink target path for venv

Virtual Environment Sub-Options:
  [--system-site-packages]              Set venv '--system-site-packages' option.
  [-f | --venv-force-remove]            (Default ON) Force remove and recreate virtual environment
  [-r | --venv-reuse]                   (Default OFF) Reuse existing virtual environment at --venv_path if it's valid.

Added by the Python port:
  [--dry-run]                           Run checks and print the plan without mutating the system.

Examples:
  install.py
  install.py --target=all
  install.py --target=dx_com
  install.py --target=dx_tron
  install.py --docker_volume_path=/path/to/docker/volume
  install.py --venv_path=./my_venv
  install.py --venv_path=./existing_venv --venv-reuse
  install.py --venv_path=./old_venv --venv-force-remove
"""


def print_usage() -> None:
    # bash printed the banner via bare `echo -e` -> stdout; keep that stream.
    print(USAGE, file=sys.stdout)


def parse_args(argv: list[str]) -> Config:
    cfg = Config()
    for arg in argv:
        if arg.startswith("--target="):
            cfg.target_pkg = arg.split("=", 1)[1]
        elif arg.startswith("--archive_mode="):
            cfg.archive_mode = arg.split("=", 1)[1]
        elif arg.startswith("--docker_volume_path="):
            cfg.docker_volume_path = arg.split("=", 1)[1]
        elif arg.startswith("--python_version="):
            cfg.python_version = arg.split("=", 1)[1]
        elif arg.startswith("--venv_path="):
            cfg.venv_path_override = arg.split("=", 1)[1]
        elif arg.startswith("--venv_symlink_target_path="):
            cfg.venv_symlink_target_path_override = arg.split("=", 1)[1]
        elif arg in ("-f", "--venv-force-remove"):
            # -f forces remove & clears reuse; -r (below) only sets reuse. Because
            # tokens are consumed in order, `-r -f` ends reuse=False (no conflict)
            # while `-f -r` and `-r` alone leave both set -> validate_environment
            # conflict abort — exactly the shell's order-sensitive behavior.
            cfg.force_remove_venv = True
            cfg.reuse_venv = False
        elif arg in ("-r", "--venv-reuse"):
            cfg.reuse_venv = True
        elif arg == "--system-site-packages":
            cfg.system_site_packages = True
        elif arg == "--verbose":
            cfg.enable_debug_logs = True
        elif arg == "--force":
            cfg.force_args = "--force"
        elif arg.startswith("--force="):
            cfg.force_args = "" if arg.split("=", 1)[1] == "false" else "--force"
        elif arg.startswith("--pypi="):
            v = arg.split("=", 1)[1]
            if v == "true":
                cfg.use_pypi = True
            elif v == "false":
                cfg.use_pypi = False
            else:
                die(f"Invalid value for --pypi: '{v}'. Use 'true' or 'false'.", with_usage=True)
        elif arg == "--dry-run":
            cfg.dry_run = True
        elif arg == "--help":
            print_usage()
            raise InstallAbort(0)
        else:
            die(f"Unknown option: {arg}", with_usage=True)
    return cfg


def main(argv: list[str]) -> int:
    global ui
    ui = UI(debug=("--verbose" in argv))
    ui.header("installer  (python port of install.sh)")
    os.chdir(PROJECT_ROOT)
    try:
        cfg = parse_args(argv)
        ui.debug_enabled = cfg.enable_debug_logs
        load_properties(cfg)
        run_main(cfg)
    except InstallAbort as e:
        return int(e.code) if e.code is not None else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
