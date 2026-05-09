# Quartus II MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that gives **Claude** full access to **Intel/Altera Quartus II 13.1** — create projects, compile designs, assign pins, run timing analysis, program FPGAs via JTAG, and more, all from a Claude conversation.

---

## What You Can Do

Once connected, you can talk to Claude naturally:

> _"Create a new Cyclone IV project called `blink`, add my `blink.v` file, assign the clock to PIN_R8 and the LED to PIN_A15, then compile it and program my FPGA."_

Claude will call the appropriate tools and report back results, errors, and warnings — no manual GUI clicks needed.

---

## Requirements

| Requirement                                                                                                                                          | Version                      |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| [Quartus II](https://www.intel.com/content/www/us/en/software-kit/711791/intel-quartus-ii-web-edition-design-software-version-13-1-for-windows.html) | 13.1 (Web Edition or higher) |
| Python                                                                                                                                               | 3.10 or later                |
| [Claude Code](https://claude.ai/download)                                                                                                            | Latest                       |

> **Note:** Quartus II must be installed on the same machine running Claude Code. The default install path assumed is `C:\altera\13.1\`. See [Configuration](#configuration) if yours differs.

---

## Installation

### 1. Clone this repository

```bash
git clone https://github.com/irumvag/quartus_mcp_server.git
cd quartus-mcp-server
```

### 2. Install the Python dependency

```bash
pip install mcp>=1.0.0
```

Or use the requirements file:

```bash
pip install -r requirements.txt
```

### 3. Verify Quartus is reachable

Open a terminal and confirm the Quartus shell responds:

```
C:\altera\13.1\quartus\bin64\quartus_sh.exe --version
```

You should see output like `Version 13.1.0 Build 162 ...`.

---

## Configuration

### Option A — Project-level (recommended)

The repository already includes `.claude/settings.json`. Open it and **update the path** to point to `quartus_mcp_server.py` on your machine:

```json
{
  "mcpServers": {
    "quartus": {
      "command": "python",
      "args": ["C:\\path\\to\\your\\quartus-mcp-server\\quartus_mcp_server.py"],
      "env": {
        "QUARTUS_ROOTDIR": "C:\\altera\\13.1\\quartus",
        "QUARTUS_ROOTDIR_OVERRIDE": "C:\\altera\\13.1\\quartus"
      }
    }
  }
}
```

> **If Quartus is installed in a different location**, also update `QUARTUS_ROOTDIR` and `QUARTUS_ROOTDIR_OVERRIDE` to match.

### Option B — Global (available in all Claude Code sessions)

Edit (or create) `C:\Users\YOUR_NAME\.claude\settings.json`:

```json
{
  "mcpServers": {
    "quartus": {
      "command": "python",
      "args": ["C:\\path\\to\\your\\quartus-mcp-server\\quartus_mcp_server.py"],
      "env": {
        "QUARTUS_ROOTDIR": "C:\\altera\\13.1\\quartus",
        "QUARTUS_ROOTDIR_OVERRIDE": "C:\\altera\\13.1\\quartus"
      }
    }
  }
}
```

### If Quartus is not in `C:\altera\13.1\`

Open `quartus_mcp_server.py` and update line 22:

```python
QUARTUS_BIN = r"C:\altera\13.1\quartus\bin64"   # ← change this
```

### Activate

**Restart Claude Code.** The tools will appear with the `quartus__` prefix (e.g. `quartus__compile_project`).

---

## Verifying the Connection

After restarting Claude Code, open a session in this project's directory and ask Claude:

> _"List the available Quartus device families."_

Claude will call `get_device_families` and return a list like `Cyclone IV E`, `Stratix V`, etc. If it does, everything is working.

---

## Available Tools (38)

### Project Management

| Tool               | Description                                            |
| ------------------ | ------------------------------------------------------ |
| `create_project`   | Create a new project (name, directory, family, device) |
| `open_project`     | Open and verify an existing project                    |
| `close_project`    | Close the current project                              |
| `get_project_info` | Read family, device, top-level entity                  |
| `list_projects`    | Find all `.qpf` files in a directory tree              |
| `archive_project`  | Archive a project to a `.qar` file                     |

### Compilation

| Tool                       | Description                                                       |
| -------------------------- | ----------------------------------------------------------------- |
| `compile_project`          | Run full or partial flow (`full` / `map` / `fit` / `asm` / `sta`) |
| `run_analysis_synthesis`   | Run Analysis & Synthesis (`quartus_map`)                          |
| `run_fitter`               | Run Fitter / Place & Route (`quartus_fit`)                        |
| `run_assembler`            | Run Assembler — produces `.sof` and `.pof` files                  |
| `get_compilation_status`   | Check last compile status and list output artifacts               |
| `get_compilation_messages` | Extract errors and warnings from report files                     |

### Timing Analysis

| Tool                  | Description                                |
| --------------------- | ------------------------------------------ |
| `run_timing_analysis` | Run Static Timing Analysis (`quartus_sta`) |
| `get_timing_summary`  | Read the `.sta.summary` report             |
| `get_clock_summary`   | Get all clock names and periods            |
| `get_timing_paths`    | Get timing paths between two nodes         |

### Pin Assignments & Device

| Tool                     | Description                                                     |
| ------------------------ | --------------------------------------------------------------- |
| `get_device_families`    | List all supported device families                              |
| `get_devices`            | List all part numbers for a family                              |
| `get_pin_assignments`    | Read all `set_location_assignment` entries                      |
| `set_pin_assignment`     | Assign a signal to a pin (+ optional I/O standard)              |
| `remove_pin_assignment`  | Remove a pin location assignment                                |
| `get_global_assignments` | Read all `set_global_assignment` entries                        |
| `set_global_assignment`  | Write a global assignment (e.g. `SDC_FILE`, `TOP_LEVEL_ENTITY`) |

### JTAG / Programmer

| Tool                    | Description                                      |
| ----------------------- | ------------------------------------------------ |
| `detect_jtag_devices`   | Detect JTAG-connected devices                    |
| `get_programmer_cables` | List available cables (USB-Blaster, etc.)        |
| `program_device`        | Program an FPGA using the latest `.sof` via JTAG |

### Reports & Analysis

| Tool                    | Description                                   |
| ----------------------- | --------------------------------------------- |
| `get_flow_summary`      | Read the `.flow.rpt` compilation summary      |
| `get_resource_usage`    | Get LUT/register/memory utilization           |
| `get_power_report`      | Run `quartus_pow` and return the power report |
| `run_design_rule_check` | Run `quartus_drc`                             |

### File Management

| Tool                       | Description                                                |
| -------------------------- | ---------------------------------------------------------- |
| `list_project_files`       | List source files registered in the `.qsf`                 |
| `add_file_to_project`      | Add a file (`VERILOG_FILE`, `VHDL_FILE`, `SDC_FILE`, etc.) |
| `remove_file_from_project` | Remove a file from the `.qsf`                              |
| `read_qsf`                 | Read the raw `.qsf` settings file                          |

### Tcl Scripting

| Tool                  | Description                                        |
| --------------------- | -------------------------------------------------- |
| `run_tcl_script`      | Run a `.tcl` file through `quartus_sh -t`          |
| `execute_tcl_command` | Run arbitrary inline Tcl code through `quartus_sh` |

---

## Example Conversations

**Create and compile a project:**

```
You: Create a Cyclone IV E project called "counter" in C:\projects\counter using device EP4CE6E22C8
Claude: [calls create_project] ✓ Project created.

You: Add C:\projects\counter\counter.v to the project
Claude: [calls add_file_to_project] ✓ File added.

You: Compile it
Claude: [calls compile_project] ✓ Compilation successful. 0 errors, 3 warnings.
```

**Check resource usage after compile:**

```
You: How many logic elements does my design use?
Claude: [calls get_resource_usage] → 42 / 6272 logic elements (1%)
```

**Assign pins from a pin table:**

```
You: Assign clk to PIN_R8, reset to PIN_J15, led[0] to PIN_A15 with 3.3-V LVTTL
Claude: [calls set_pin_assignment × 3] ✓ All pins assigned.
```

**Program the FPGA:**

```
You: Program my FPGA via USB-Blaster
Claude: [calls detect_jtag_devices, then program_device] ✓ Programming successful.
```

---

## File Structure

```
quartus-mcp-server/
├── quartus_mcp_server.py   # MCP server — all 38 tools
├── requirements.txt        # pip dependency (mcp>=1.0.0)
├── .claude/
│   └── settings.json       # Registers server with Claude Code
└── README.md               # This file
```

---

## Troubleshooting

**"Executable not found" errors**

- Confirm Quartus is installed and `QUARTUS_BIN` in `quartus_mcp_server.py` points to your `bin64` folder.

**"No .qpf file found in directory"**

- Pass the full path to either the `.qpf` file or the directory that contains it.

**Tools not appearing in Claude Code**

- Make sure `.claude/settings.json` is in the project directory you opened in Claude Code, **or** that you updated the global `~/.claude/settings.json`.
- Restart Claude Code after any settings change.

**Compilation hangs**

- Full compilation has a 600-second timeout. Very large designs may exceed this. Edit `timeout=600` in `tool_compile_project` to increase it.

**`mcp` import error on startup**

- Run `pip install mcp` again in the same Python environment Claude Code uses.

---

## License

MIT — free to use, modify, and distribute.

---

## Contributing

Pull requests are welcome. To add a new tool:

1. Write the implementation function (`tool_your_name(...)`) in `quartus_mcp_server.py`
2. Add a `Tool(...)` entry to the `TOOLS` list
3. Add a dispatch entry to `TOOL_HANDLERS`
4. Update the table in this README
