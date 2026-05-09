#!/usr/bin/env python3
"""
Quartus II 13.1 MCP Server — v2
Exposes Quartus II 13.1 (64-bit) to Claude Code via the Model Context Protocol.

Design:
- FastMCP API (mcp >= 1.0)
- Stateless: every call opens -> acts -> closes (avoids .qpf.lck conflicts)
- Read operations parse QSF/RPT files directly in Python (no Tcl needed)
- Tcl / quartus executables used only where genuinely required
- ALL logging goes to stderr; stdout is reserved for the MCP JSON-RPC wire
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging — stderr ONLY
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [quartus-mcp] %(levelname)s %(message)s",
)
log = logging.getLogger("quartus-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QUARTUS_BIN = r"C:\altera\13.1\quartus\bin64"
QUARTUS_SH  = str(Path(QUARTUS_BIN) / "quartus_sh.exe")
QUARTUS_MAP = str(Path(QUARTUS_BIN) / "quartus_map.exe")
QUARTUS_FIT = str(Path(QUARTUS_BIN) / "quartus_fit.exe")
QUARTUS_ASM = str(Path(QUARTUS_BIN) / "quartus_asm.exe")
QUARTUS_STA = str(Path(QUARTUS_BIN) / "quartus_sta.exe")
QUARTUS_PGM = str(Path(QUARTUS_BIN) / "quartus_pgm.exe")
QUARTUS_POW = str(Path(QUARTUS_BIN) / "quartus_pow.exe")
QUARTUS_DRC = str(Path(QUARTUS_BIN) / "quartus_drc.exe")

DEFAULT_PROJECT_DIR = r"C:\Users\Chairman\Documents\1CST_UR Room\zero\Quartus2"

QUARTUS_ENV: dict = {
    **os.environ,
    "QUARTUS_ROOTDIR": r"C:\altera\13.1\quartus",
    "QUARTUS_ROOTDIR_OVERRIDE": r"C:\altera\13.1\quartus",
}

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP("Quartus II 13.1")

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def run_quartus(cmd: list, cwd: Optional[str] = None, timeout: int = 300) -> dict:
    """Run a Quartus II executable and return {success, stdout, stderr, returncode}."""
    log.info("run_quartus: %s (cwd=%s, timeout=%ds)", Path(cmd[0]).name, cwd, timeout)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=QUARTUS_ENV,
        )
        log.info("returncode=%d", result.returncode)
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        log.warning("Timed out after %ds: %s", timeout, cmd[0])
        return {"success": False, "stdout": "",
                "stderr": f"Timed out after {timeout}s", "returncode": -1}
    except FileNotFoundError as e:
        log.error("Executable not found: %s", e)
        return {"success": False, "stdout": "",
                "stderr": f"Executable not found: {e}", "returncode": -1}
    except Exception as e:
        log.error("run_quartus error: %s", e)
        return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}


def run_tcl(tcl_code: str, cwd: Optional[str] = None, timeout: int = 300) -> dict:
    """Write tcl_code to a temp file and run it through quartus_sh -t."""
    work_dir = cwd or DEFAULT_PROJECT_DIR
    os.makedirs(work_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tcl", delete=False, dir=work_dir
    ) as tf:
        tf.write(tcl_code)
        tcl_path = tf.name
    log.info("run_tcl: temp=%s", tcl_path)
    try:
        return run_quartus([QUARTUS_SH, "-t", tcl_path], cwd=work_dir, timeout=timeout)
    finally:
        try:
            os.unlink(tcl_path)
        except OSError:
            pass


def resolve_project(project_path: str) -> tuple:
    """Return (qpf_path, project_dir, revision) from a .qpf path or directory."""
    p = Path(project_path)
    if p.suffix.lower() == ".qpf":
        if not p.exists():
            raise ValueError(f".qpf file not found: {p}")
        qpf = p
    elif p.is_dir():
        qpf_files = list(p.glob("*.qpf"))
        if not qpf_files:
            raise ValueError(f"No .qpf file found in directory: {p}")
        qpf = sorted(qpf_files)[0]
    else:
        raise ValueError(f"project_path must be a .qpf file or directory: {project_path}")
    revision = _read_revision(str(qpf))
    return str(qpf), str(qpf.parent), revision


def _read_revision(qpf_path: str) -> str:
    """Extract the revision name from a .qpf file."""
    text = Path(qpf_path).read_text(errors="replace")
    m = re.search(r'^PROJECT_REVISION\s*=\s*"?(\S+?)"?\s*$', text, re.MULTILINE)
    return m.group(1).rstrip('"') if m else Path(qpf_path).stem


def find_qsf(proj_dir: str, revision: str) -> Optional[str]:
    """Return path to the project's .qsf file, or None if not found."""
    qsf = Path(proj_dir) / f"{revision}.qsf"
    if qsf.exists():
        return str(qsf)
    qsfs = sorted(Path(proj_dir).glob("*.qsf"))
    return str(qsfs[0]) if qsfs else None


def parse_qsf(qsf_path: str) -> list:
    """
    Parse a QSF file and return a list of dicts: {name, value}.
    Handles both quoted and unquoted values.
    """
    assignments = []
    try:
        for line in Path(qsf_path).read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'set_global_assignment\s+-name\s+(\S+)\s+(.*)', line)
            if m:
                name = m.group(1)
                val  = m.group(2).strip().strip('"')
                assignments.append({"name": name, "value": val})
    except OSError:
        pass
    return assignments


def parse_qsf_pins(qsf_path: str) -> list:
    """Extract set_location_assignment lines from a QSF file."""
    pins = []
    try:
        for line in Path(qsf_path).read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'set_location_assignment\s+(PIN_\S+)\s+-to\s+(\S+)', line)
            if m:
                pins.append({"location": m.group(1), "signal": m.group(2)})
    except OSError:
        pass
    return pins


def j(data: Any) -> str:
    """JSON-encode data to a string."""
    return json.dumps(data, indent=2)


def truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]


# ---------------------------------------------------------------------------
# 1. Project Management
# ---------------------------------------------------------------------------

@mcp.tool()
def create_project(name: str, directory: str, family: str, device: str) -> str:
    """Create a new Quartus II project with the given device family and part number.

    Args:
        name: Project name (also used as top-level entity name)
        directory: Directory where the project will be created
        family: Device family e.g. 'Cyclone IV E'
        device: Device part number e.g. 'EP4CE115F29C7'
    """
    os.makedirs(directory, exist_ok=True)
    tcl = textwrap.dedent(f"""\
        package require ::quartus::project
        cd {{{directory}}}
        if {{[project_exists {{{name}}}]}} {{
            project_open -revision {{{name}}} {{{name}}}
        }} else {{
            project_new -revision {{{name}}} {{{name}}}
        }}
        set_global_assignment -name FAMILY {{{family}}}
        set_global_assignment -name DEVICE {{{device}}}
        set_global_assignment -name TOP_LEVEL_ENTITY {{{name}}}
        export_assignments
        project_close
        puts "PROJECT_CREATED:{name}"
    """)
    r = run_tcl(tcl, cwd=directory)
    if "PROJECT_CREATED" in r["stdout"]:
        return j({"created": True, "project": name, "directory": directory,
                  "family": family, "device": device})
    return j({"error": r["stderr"][-800:] or r["stdout"][-400:] or "Unknown error"})


@mcp.tool()
def get_project_info(project_path: str) -> str:
    """Get metadata about a Quartus II project: family, device, top-level entity, file count.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    info: dict = {"revision": revision, "qpf": qpf, "directory": proj_dir}
    qsf_path = find_qsf(proj_dir, revision)
    if qsf_path:
        info["qsf"] = qsf_path
        assignments = parse_qsf(qsf_path)
        for a in assignments:
            if a["name"] in ("FAMILY", "DEVICE", "TOP_LEVEL_ENTITY"):
                info[a["name"].lower()] = a["value"]
        info["source_file_count"] = sum(1 for a in assignments if a["name"].endswith("_FILE"))
    return j(info)


@mcp.tool()
def list_projects(directory: str) -> str:
    """List all Quartus II projects (.qpf files) recursively in a directory.

    Args:
        directory: Root directory to search
    """
    p = Path(directory)
    if not p.is_dir():
        return j({"error": f"Directory not found: {directory}"})
    projects = [
        {"name": qpf.stem, "qpf": str(qpf), "directory": str(qpf.parent)}
        for qpf in sorted(p.rglob("*.qpf"))
    ]
    return j({"directory": directory, "projects": projects, "count": len(projects)})


@mcp.tool()
def close_project() -> str:
    """Close the current project. (Informational — server is stateless; each tool call manages its own session.)"""
    return j({"note": "Server is stateless. Each tool call opens and closes its own project session."})


@mcp.tool()
def archive_project(project_path: str, output_path: str) -> str:
    """Archive a Quartus II project to a .qar file.

    Args:
        project_path: Path to .qpf file or project directory
        output_path: Destination .qar archive file path
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    tcl = textwrap.dedent(f"""\
        package require ::quartus::project
        package require ::quartus::flow
        project_open -revision {{{revision}}} {{{qpf}}}
        execute_flow -archive -archive_file {{{output_path}}}
        project_close
        puts "ARCHIVED"
    """)
    r = run_tcl(tcl, cwd=proj_dir, timeout=180)
    return j({"archived": "ARCHIVED" in r["stdout"], "output_path": output_path,
              "stdout": r["stdout"][-1000:]})


# ---------------------------------------------------------------------------
# 2. Compilation & Synthesis
# ---------------------------------------------------------------------------

@mcp.tool()
def compile_project(project_path: str, flow: str = "full") -> str:
    """Compile a Quartus II project. Runs the specified flow stage.

    Args:
        project_path: Path to .qpf file or project directory
        flow: Stage to run — 'full' (all stages), 'map' (analysis+synthesis),
              'fit' (place and route), 'asm' (assembler / generate .sof),
              'sta' (static timing analysis)
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})

    flow = flow.lower().strip()
    if flow == "full":
        cmd = [QUARTUS_SH, "--flow", "compile", revision]
        timeout = 900
    elif flow == "map":
        cmd = [QUARTUS_MAP, revision, "--read_settings_files=on", "--write_settings_files=off"]
        timeout = 300
    elif flow == "fit":
        cmd = [QUARTUS_FIT, revision, "--read_settings_files=on", "--write_settings_files=off"]
        timeout = 300
    elif flow == "asm":
        cmd = [QUARTUS_ASM, revision, "--read_settings_files=on", "--write_settings_files=off"]
        timeout = 180
    elif flow == "sta":
        cmd = [QUARTUS_STA, revision, "--do_report_timing"]
        timeout = 180
    else:
        return j({"error": f"Unknown flow '{flow}'. Valid: full, map, fit, asm, sta"})

    r = run_quartus(cmd, cwd=proj_dir, timeout=timeout)
    return j({
        "flow": flow, "success": r["success"], "returncode": r["returncode"],
        "stdout": truncate(r["stdout"], 5000),
        "stderr": truncate(r["stderr"], 2000),
    })


@mcp.tool()
def run_analysis_synthesis(project_path: str) -> str:
    """Run Analysis and Synthesis only (quartus_map) on a project.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    r = run_quartus(
        [QUARTUS_MAP, revision, "--read_settings_files=on", "--write_settings_files=off"],
        cwd=proj_dir, timeout=300,
    )
    return j({"success": r["success"], "stdout": truncate(r["stdout"], 4000),
              "stderr": r["stderr"][-1000:]})


@mcp.tool()
def run_fitter(project_path: str) -> str:
    """Run the Fitter / Place & Route (quartus_fit) on a project.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    r = run_quartus(
        [QUARTUS_FIT, revision, "--read_settings_files=on", "--write_settings_files=off"],
        cwd=proj_dir, timeout=300,
    )
    return j({"success": r["success"], "stdout": truncate(r["stdout"], 4000),
              "stderr": r["stderr"][-1000:]})


@mcp.tool()
def run_assembler(project_path: str) -> str:
    """Run the Assembler (quartus_asm) to generate .sof and .pof programming files.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    r = run_quartus(
        [QUARTUS_ASM, revision, "--read_settings_files=on", "--write_settings_files=off"],
        cwd=proj_dir, timeout=180,
    )
    sof_files = [str(f) for f in Path(proj_dir).glob("**/*.sof")]
    pof_files = [str(f) for f in Path(proj_dir).glob("**/*.pof")]
    return j({"success": r["success"], "sof_files": sof_files, "pof_files": pof_files,
              "stdout": r["stdout"][-2000:]})


@mcp.tool()
def get_compilation_status(project_path: str) -> str:
    """Check the last compilation status and list generated artifact files.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    p = Path(proj_dir)
    status: dict = {"directory": proj_dir, "artifacts": {}}
    for pattern, key in [
        ("*.flow.rpt", "flow_report"), ("*.sta.summary", "sta_summary"),
        ("*.map.rpt",  "map_report"),  ("*.fit.rpt",    "fit_report"),
        ("*.asm.rpt",  "asm_report"),  ("*.sof",        "sof"),
        ("*.pof",      "pof"),
    ]:
        files = sorted(p.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            status["artifacts"][key] = str(files[0])

    flow_rpt = status["artifacts"].get("flow_report")
    if flow_rpt:
        text = Path(flow_rpt).read_text(errors="replace")
        status["flow_passed"] = "Full Compilation was successful" in text
        keywords = ("Flow Status", "Quartus II Version", "Revision Name", "Top-level Entity",
                    "Family", "Device", "Total logic", "Total registers",
                    "Total pins", "Total memory", "Logic utilization")
        status["summary_lines"] = [
            line.strip() for line in text.splitlines()
            if any(kw in line for kw in keywords)
        ][:25]
    return j(status)


@mcp.tool()
def get_compilation_messages(project_path: str) -> str:
    """Extract errors, warnings, and critical warnings from all compilation report files.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    messages: dict = {"errors": [], "warnings": [], "criticals": []}
    for rpt_file in sorted(Path(proj_dir).glob("*.rpt")):
        try:
            for line in rpt_file.read_text(errors="replace").splitlines():
                ls = line.strip()
                if re.match(r'Error\s*[\(:]', ls):
                    messages["errors"].append(ls)
                elif re.match(r'Critical Warning', ls):
                    messages["criticals"].append(ls)
                elif re.match(r'Warning\s*[\(:]', ls):
                    messages["warnings"].append(ls)
        except OSError:
            pass
    for key in messages:
        messages[key] = list(dict.fromkeys(messages[key]))[:100]
    return j({**messages,
              "error_count": len(messages["errors"]),
              "warning_count": len(messages["warnings"]),
              "critical_count": len(messages["criticals"])})


# ---------------------------------------------------------------------------
# 3. Timing Analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def run_timing_analysis(project_path: str) -> str:
    """Run Static Timing Analysis (quartus_sta) on a compiled project.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    r = run_quartus([QUARTUS_STA, revision, "--do_report_timing"],
                    cwd=proj_dir, timeout=180)
    return j({"success": r["success"],
              "stdout": truncate(r["stdout"], 4000),
              "stderr": r["stderr"][-1000:]})


@mcp.tool()
def get_timing_summary(project_path: str) -> str:
    """Read and return the timing analysis summary (.sta.summary file).

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    summaries = sorted(Path(proj_dir).glob("*.sta.summary"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    if not summaries:
        return j({"error": "No .sta.summary found. Run run_timing_analysis first."})
    text = summaries[0].read_text(errors="replace")
    return j({"file": str(summaries[0]), "content": text[:8000]})


@mcp.tool()
def get_clock_summary(project_path: str) -> str:
    """Get clock definitions and their frequency/period from the timing analysis summary.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    summaries = sorted(Path(proj_dir).glob("*.sta.summary"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    clocks = []
    if summaries:
        text = summaries[0].read_text(errors="replace")
        # Match lines like: ; clk_name ; 1 ; 50.0 MHz ; 20.0 ns ;
        for line in text.splitlines():
            m = re.search(
                r';\s*(\S+)\s*;\s*\d+\s*;\s*([\d.]+)\s*MHz\s*;\s*([\d.]+)\s*ns', line
            )
            if m:
                clocks.append({"clock": m.group(1),
                                "frequency_mhz": m.group(2),
                                "period_ns": m.group(3)})
    return j({"clocks": clocks,
              "sta_summary_file": str(summaries[0]) if summaries else None})


@mcp.tool()
def get_timing_paths(project_path: str, from_node: str, to_node: str) -> str:
    """Get timing path information between two nodes via quartus_sta Tcl API.

    Args:
        project_path: Path to .qpf file or project directory
        from_node: Source node name (wildcards * supported)
        to_node: Destination node name (wildcards * supported)
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    tcl = textwrap.dedent(f"""\
        package require ::quartus::sta
        project_open {{{qpf}}}
        create_timing_netlist
        read_sdc
        update_timing_netlist
        set paths [get_timing_paths -from {{{from_node}}} -to {{{to_node}}} -npaths 5]
        foreach_in_collection path $paths {{
            set delay [get_timing_path_info -data_path_delay $path]
            set slack [get_timing_path_info -slack $path]
            puts "PATH:$delay:$slack"
        }}
        delete_timing_netlist
        project_close
    """)
    r = run_tcl(tcl, cwd=proj_dir, timeout=120)
    paths = []
    for line in r["stdout"].splitlines():
        if line.startswith("PATH:"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                paths.append({"data_path_delay_ns": parts[1], "slack_ns": parts[2]})
    return j({"from": from_node, "to": to_node, "paths": paths,
              "raw_output": r["stdout"][:2000]})


# ---------------------------------------------------------------------------
# 4. Pin Assignment & Device
# ---------------------------------------------------------------------------

@mcp.tool()
def get_device_families() -> str:
    """List all FPGA/CPLD device families supported by this Quartus II installation."""
    tcl = textwrap.dedent("""\
        package require ::quartus::device
        foreach f [get_family_list] {
            puts "FAMILY:$f"
        }
    """)
    r = run_tcl(tcl, cwd=DEFAULT_PROJECT_DIR)
    families = [line.split(":", 1)[1] for line in r["stdout"].splitlines()
                if line.startswith("FAMILY:")]
    if not families:
        return j({"error": "Could not retrieve families", "stderr": r["stderr"][-500:]})
    return j({"families": families, "count": len(families)})


@mcp.tool()
def get_devices(family: str) -> str:
    """List all device part numbers for a given device family.

    Args:
        family: Device family name e.g. 'Cyclone IV E'
    """
    tcl = textwrap.dedent(f"""\
        package require ::quartus::device
        foreach p [get_part_list -family {{{family}}}] {{
            puts "DEVICE:$p"
        }}
    """)
    r = run_tcl(tcl, cwd=DEFAULT_PROJECT_DIR)
    devices = [line.split(":", 1)[1] for line in r["stdout"].splitlines()
               if line.startswith("DEVICE:")]
    return j({"family": family, "devices": devices, "count": len(devices)})


@mcp.tool()
def get_pin_assignments(project_path: str) -> str:
    """Get all pin location assignments for a project, parsed from the .qsf file.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    qsf_path = find_qsf(proj_dir, revision)
    if not qsf_path:
        return j({"error": f"No .qsf file found in {proj_dir}"})
    pins = parse_qsf_pins(qsf_path)
    return j({"pins": pins, "count": len(pins), "qsf": qsf_path})


@mcp.tool()
def set_pin_assignment(project_path: str, pin_name: str,
                       pin_location: str, io_standard: str = "") -> str:
    """Set a pin location assignment for a signal in the project.

    Args:
        project_path: Path to .qpf file or project directory
        pin_name: Signal/port name to assign
        pin_location: Pin location e.g. PIN_A1
        io_standard: I/O standard e.g. '3.3-V LVTTL' (optional)
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    io_line = (
        f'set_instance_assignment -name IO_STANDARD "{io_standard}" -to {{{pin_name}}}'
        if io_standard else ""
    )
    tcl = textwrap.dedent(f"""\
        package require ::quartus::project
        project_open -revision {{{revision}}} {{{qpf}}}
        set_location_assignment {pin_location} -to {{{pin_name}}}
        {io_line}
        export_assignments
        project_close
        puts "PIN_SET"
    """)
    r = run_tcl(tcl, cwd=proj_dir)
    return j({"set": "PIN_SET" in r["stdout"], "pin": pin_name,
              "location": pin_location, "io_standard": io_standard,
              "stdout": r["stdout"][-300:]})


@mcp.tool()
def remove_pin_assignment(project_path: str, pin_name: str) -> str:
    """Remove pin location and IO_STANDARD assignments for a signal (edits .qsf directly).

    Args:
        project_path: Path to .qpf file or project directory
        pin_name: Signal/port name whose pin assignment should be removed
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    qsf_path = find_qsf(proj_dir, revision)
    if not qsf_path:
        return j({"error": f"No .qsf file found in {proj_dir}"})
    text = Path(qsf_path).read_text(errors="replace")
    new_lines, removed = [], 0
    escaped = re.escape(pin_name)
    for line in text.splitlines():
        if re.search(rf'-to\s+{escaped}\s*$', line.strip()):
            removed += 1
        else:
            new_lines.append(line)
    if removed:
        Path(qsf_path).write_text("\n".join(new_lines) + "\n")
    return j({"removed": removed > 0, "lines_removed": removed, "pin": pin_name})


@mcp.tool()
def get_global_assignments(project_path: str) -> str:
    """Get all global assignments from a project's .qsf settings file.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    qsf_path = find_qsf(proj_dir, revision)
    if not qsf_path:
        return j({"error": f"No .qsf file found in {proj_dir}"})
    assignments = parse_qsf(qsf_path)
    # Group multi-value assignments (e.g. multiple source files under same key)
    by_name: dict = {}
    for a in assignments:
        nm = a["name"]
        by_name.setdefault(nm, []).append(a["value"])
    # Flatten single-value ones for readability
    simplified = {k: (v[0] if len(v) == 1 else v) for k, v in by_name.items()}
    return j({"assignments": simplified, "total": len(assignments), "qsf": qsf_path})


@mcp.tool()
def set_global_assignment(project_path: str, name: str, value: str) -> str:
    """Set a global assignment in a project (e.g. DEVICE, TOP_LEVEL_ENTITY, SDC_FILE).

    Args:
        project_path: Path to .qpf file or project directory
        name: Assignment name e.g. DEVICE, SDC_FILE
        value: Assignment value
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    tcl = textwrap.dedent(f"""\
        package require ::quartus::project
        project_open -revision {{{revision}}} {{{qpf}}}
        set_global_assignment -name {{{name}}} {{{value}}}
        export_assignments
        project_close
        puts "GLOBAL_SET"
    """)
    r = run_tcl(tcl, cwd=proj_dir)
    return j({"set": "GLOBAL_SET" in r["stdout"], "name": name, "value": value,
              "stdout": r["stdout"][-300:]})


# ---------------------------------------------------------------------------
# 5. Programmer / JTAG
# ---------------------------------------------------------------------------

@mcp.tool()
def detect_jtag_devices() -> str:
    """Detect JTAG-connected devices using jtagconfig or quartus_pgm."""
    jtagconfig = str(Path(QUARTUS_BIN) / "jtagconfig.exe")
    if Path(jtagconfig).exists():
        r = run_quartus([jtagconfig], timeout=30)
    else:
        r = run_quartus([QUARTUS_PGM, "--auto", "--list"], timeout=30)
    return j({"output": r["stdout"], "stderr": r["stderr"], "success": r["success"]})


@mcp.tool()
def get_programmer_cables() -> str:
    """List available programming cables (USB-Blaster, etc.)."""
    r = run_quartus([QUARTUS_PGM, "--list"], timeout=30)
    lines = [l.strip() for l in r["stdout"].splitlines() if l.strip()]
    return j({"cables": lines, "raw_output": r["stdout"]})


@mcp.tool()
def program_device(project_path: str, cable: str = "USB-Blaster",
                   device_index: int = 1) -> str:
    """Program an FPGA via JTAG using the most recently compiled .sof file.

    Args:
        project_path: Path to .qpf file or project directory
        cable: Cable name from get_programmer_cables (default: USB-Blaster)
        device_index: JTAG chain device index, 1-based (default: 1)
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    sof_files = sorted(Path(proj_dir).glob("**/*.sof"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    if not sof_files:
        return j({"error": "No .sof file found. Run compile_project first."})
    sof_path = str(sof_files[0])
    r = run_quartus(
        [QUARTUS_PGM, "-c", cable, "-m", "JTAG", "-o", f"p;{sof_path}@{device_index}"],
        cwd=proj_dir, timeout=120,
    )
    return j({"success": r["success"], "sof": sof_path, "cable": cable,
              "device_index": device_index,
              "stdout": r["stdout"], "stderr": r["stderr"]})


# ---------------------------------------------------------------------------
# 6. Reports & Analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def get_flow_summary(project_path: str) -> str:
    """Read the full compilation flow summary report (.flow.rpt).

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    rpts = sorted(Path(proj_dir).glob("*.flow.rpt"),
                  key=lambda f: f.stat().st_mtime, reverse=True)
    if not rpts:
        return j({"error": "No .flow.rpt found. Run compile_project first."})
    text = rpts[0].read_text(errors="replace")
    # Extract the summary section (first ~100 lines usually contain it)
    summary_lines, capture = [], False
    for line in text.splitlines():
        if "Flow Summary" in line or "Flow Status" in line:
            capture = True
        if capture:
            summary_lines.append(line)
            if len(summary_lines) > 100:
                break
    return j({"file": str(rpts[0]),
              "summary": "\n".join(summary_lines),
              "total_lines": len(text.splitlines())})


@mcp.tool()
def get_resource_usage(project_path: str) -> str:
    """Get FPGA resource utilization (LEs, registers, memory, pins) from compilation reports.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    resources: dict = {}
    for pattern in ("*.fit.rpt", "*.map.rpt"):
        rpts = sorted(Path(proj_dir).glob(pattern),
                      key=lambda f: f.stat().st_mtime, reverse=True)
        if not rpts:
            continue
        text = rpts[0].read_text(errors="replace")
        in_section = False
        for line in text.splitlines():
            if "Resource Usage Summary" in line or "Total logic elements" in line:
                in_section = True
            if in_section and ";" in line:
                parts = [p.strip() for p in line.split(";")]
                if len(parts) >= 3 and parts[1] and parts[2]:
                    resources[parts[1]] = parts[2]
            if in_section and not line.strip() and resources:
                break
        if resources:
            return j({"resources": resources, "source": str(rpts[0])})
    return j({"resources": resources, "note": "No fit or map report found"})


@mcp.tool()
def read_report_file(project_path: str, report_type: str = "flow") -> str:
    """Read a specific Quartus compilation report file.

    Args:
        project_path: Path to .qpf file or project directory
        report_type: One of: 'flow', 'map', 'fit', 'asm', 'sta', 'pow'
    """
    try:
        _, proj_dir, _ = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    pattern_map = {
        "flow": "*.flow.rpt", "map": "*.map.rpt", "fit": "*.fit.rpt",
        "asm": "*.asm.rpt",   "sta": "*.sta.summary", "pow": "*.pow.rpt",
    }
    pattern = pattern_map.get(report_type, f"*.{report_type}.rpt")
    rpts = sorted(Path(proj_dir).glob(pattern),
                  key=lambda f: f.stat().st_mtime, reverse=True)
    if not rpts:
        return j({"error": f"No {report_type} report found in {proj_dir}"})
    text = rpts[0].read_text(errors="replace")
    return j({"file": str(rpts[0]),
              "content": text[:12000],
              "total_chars": len(text),
              "note": "content truncated to 12000 chars" if len(text) > 12000 else ""})


@mcp.tool()
def get_power_report(project_path: str) -> str:
    """Run power analysis (quartus_pow) and return the power estimate report.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    r = run_quartus([QUARTUS_POW, revision], cwd=proj_dir, timeout=180)
    pow_rpts = sorted(Path(proj_dir).glob("*.pow.rpt"),
                      key=lambda f: f.stat().st_mtime, reverse=True)
    report = (pow_rpts[0].read_text(errors="replace")[:5000]
              if pow_rpts else r["stdout"][-3000:])
    return j({"success": r["success"], "report": report})


@mcp.tool()
def run_design_rule_check(project_path: str) -> str:
    """Run the Design Rule Check (quartus_drc) on a compiled project.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    r = run_quartus([QUARTUS_DRC, revision], cwd=proj_dir, timeout=120)
    violations = [l.strip() for l in r["stdout"].splitlines()
                  if "Violation" in l or re.match(r'\s*Error', l)]
    return j({"success": r["success"], "violations": violations,
              "stdout": r["stdout"][-2000:]})


# ---------------------------------------------------------------------------
# 7. File Management (QSF-based — no Tcl needed for reads)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_project_files(project_path: str) -> str:
    """List all source files registered in a project's .qsf settings file.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    qsf_path = find_qsf(proj_dir, revision)
    if not qsf_path:
        return j({"error": f"No .qsf file found in {proj_dir}"})
    assignments = parse_qsf(qsf_path)
    files = [{"type": a["name"], "path": a["value"]}
             for a in assignments if a["name"].endswith("_FILE")]
    return j({"files": files, "count": len(files), "qsf": qsf_path})


@mcp.tool()
def add_file_to_project(project_path: str, file_path: str,
                        file_type: str = "SYSTEMVERILOG_FILE") -> str:
    """Add a source file to the project (.qsf) via Quartus Tcl.
    file_type: VERILOG_FILE, VHDL_FILE, SYSTEMVERILOG_FILE, SDC_FILE, MIF_FILE, etc.

    Args:
        project_path: Path to .qpf file or project directory
        file_path: Absolute or relative path to the source file to add
        file_type: Quartus file type assignment name (default: SYSTEMVERILOG_FILE)
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    tcl = textwrap.dedent(f"""\
        package require ::quartus::project
        project_open -revision {{{revision}}} {{{qpf}}}
        set_global_assignment -name {file_type} {{{file_path}}}
        export_assignments
        project_close
        puts "FILE_ADDED"
    """)
    r = run_tcl(tcl, cwd=proj_dir)
    return j({"added": "FILE_ADDED" in r["stdout"], "file": file_path,
              "type": file_type, "stdout": r["stdout"][-300:]})


@mcp.tool()
def remove_file_from_project(project_path: str, file_path: str) -> str:
    """Remove a source file from the project by editing the .qsf directly.

    Args:
        project_path: Path to .qpf file or project directory
        file_path: Path of the file to remove (as listed in the .qsf)
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    qsf_path = find_qsf(proj_dir, revision)
    if not qsf_path:
        return j({"error": f"No .qsf file found in {proj_dir}"})
    norm_target = file_path.replace("\\", "/").lower()
    text = Path(qsf_path).read_text(errors="replace")
    new_lines, removed = [], 0
    for line in text.splitlines():
        norm_line = line.replace("\\", "/").lower()
        if norm_target in norm_line and "_file" in norm_line:
            removed += 1
        else:
            new_lines.append(line)
    if removed:
        Path(qsf_path).write_text("\n".join(new_lines) + "\n")
    return j({"removed": removed > 0, "lines_removed": removed, "file": file_path})


@mcp.tool()
def read_qsf(project_path: str) -> str:
    """Read and return the raw content of a project's .qsf settings file.

    Args:
        project_path: Path to .qpf file or project directory
    """
    try:
        qpf, proj_dir, revision = resolve_project(project_path)
    except ValueError as e:
        return j({"error": str(e)})
    qsf_path = find_qsf(proj_dir, revision)
    if not qsf_path:
        return j({"error": f"No .qsf found in {proj_dir}"})
    text = Path(qsf_path).read_text(errors="replace")
    return j({"qsf_path": qsf_path, "content": text, "lines": len(text.splitlines())})


# ---------------------------------------------------------------------------
# 8. Tcl Execution
# ---------------------------------------------------------------------------

@mcp.tool()
def run_tcl_script(script_path: str, project_path: str = "") -> str:
    """Execute an existing Tcl script file through quartus_sh -t.

    Args:
        script_path: Absolute path to the .tcl script file
        project_path: Optional project directory for working directory context
    """
    if not Path(script_path).exists():
        return j({"error": f"Script not found: {script_path}"})
    cwd = DEFAULT_PROJECT_DIR
    if project_path:
        try:
            _, cwd, _ = resolve_project(project_path)
        except ValueError:
            if Path(project_path).is_dir():
                cwd = project_path
    r = run_quartus([QUARTUS_SH, "-t", script_path], cwd=cwd, timeout=300)
    return j({"success": r["success"],
              "stdout": truncate(r["stdout"], 6000),
              "stderr": r["stderr"][-2000:]})


@mcp.tool()
def execute_tcl_command(tcl_code: str, project_path: str = "") -> str:
    """Execute arbitrary inline Tcl code through quartus_sh -t.
    Load packages as needed, e.g.: package require ::quartus::project

    Args:
        tcl_code: The Tcl code to execute
        project_path: Optional project path for working directory context
    """
    cwd = DEFAULT_PROJECT_DIR
    if project_path:
        try:
            _, cwd, _ = resolve_project(project_path)
        except ValueError:
            if Path(project_path).is_dir():
                cwd = project_path
    r = run_tcl(tcl_code, cwd=cwd, timeout=120)
    return j({"success": r["success"],
              "stdout": truncate(r["stdout"], 6000),
              "stderr": r["stderr"][-2000:]})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Quartus II 13.1 MCP Server starting")
    log.info("Python: %s", sys.version.split()[0])
    log.info("Quartus bin: %s", QUARTUS_BIN)
    log.info("quartus_sh exists: %s", Path(QUARTUS_SH).exists())
    log.info("Default project dir: %s", DEFAULT_PROJECT_DIR)
    mcp.run()
