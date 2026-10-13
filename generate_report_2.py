#!/usr/bin/env python3
"""
Maestro Test Report Generator (Updated for BrowserStack API compatibility)
========================================================================
Wraps `maestro test` to produce a per-test-case HTML report with step-level
detail and failure screenshots.

Usage:
    python3 generate_report_2.py <flow.yaml-or-folder> [--output reports/report.html]
    python3 generate_report_2.py <flow.yaml> --commands-json <commandlogs.json> [--screenshots-dir <dir>]

Examples:
    # Run Maestro locally and build the report
    python3 generate_report_2.py flows/Login.yaml
    # Build a report from a BrowserStack /commandlogs download (no Maestro run)
    python3 generate_report_2.py examples/Login.yaml \
        --commands-json examples/browserstack_commandlogs.sample.json -o reports/example.html
"""

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# ─── Platform Detection ───────────────────────────────────────────────────


def detect_platform():
    """
    Auto-detect the connected device platform by checking for Android (adb)
    and iOS (xcrun simctl) devices.

    Returns a string: "Android", "iOS", "Android & iOS", or "Unknown".
    """
    android_connected = False
    ios_connected = False

    # Check for Android devices via adb
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines()[1:]:
                if line.strip() and "device" in line and "offline" not in line:
                    android_connected = True
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check for iOS simulators via xcrun simctl
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "booted"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if "(Booted)" in line:
                    ios_connected = True
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if android_connected and ios_connected:
        return "Android & iOS"
    elif android_connected:
        return "Android"
    elif ios_connected:
        return "iOS"
    else:
        return "Unknown"


# ─── Screenshot Compression ────────────────────────────────────────────────

_SCREENSHOT_MAX_WIDTH = 540


def _encode_screenshot(path):
    """Read a screenshot, resize if needed, and return a base64 data-URI string."""
    try:
        from PIL import Image
        import io

        img = Image.open(path)
        if img.width > _SCREENSHOT_MAX_WIDTH:
            ratio = _SCREENSHOT_MAX_WIDTH / img.width
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8"), "jpeg"
    except ImportError:
        pass

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8"), "png"


# ─── TC Boundary Parser ─────────────────────────────────────────────────────

_TC_ID = r"TC-?[\w]+(?:\s*[&,\-]\s*TC-?[\w]+)*(?:\s*\([^)]*\))?"

TC_MARKER_RE = re.compile(
    rf"^#\s*═+\s*({_TC_ID})\s*[:—–\-]\s*(.+?)\s*═*\s*$",
    re.IGNORECASE,
)

TC_BETWEEN_BORDERS_PUNCT_RE = re.compile(
    rf"^#\s+({_TC_ID})\s*[:—–\-]\s*(.+?)\s*$",
    re.IGNORECASE,
)

TC_BETWEEN_BORDERS_RE = re.compile(
    rf"^#\s+({_TC_ID})\s{{2,}}(.+?)\s*$",
    re.IGNORECASE,
)

BORDER_RE = re.compile(r"^#\s*═{5,}\s*$")


def is_yaml_command(line):
    stripped = line.strip()
    if not stripped: return False
    if stripped.startswith("#"): return False
    if stripped == "---": return False
    if stripped.startswith("appId:"): return False
    return True


def is_top_level_command(line):
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped == "---": return False
    if stripped.startswith("appId:"): return False
    if stripped.startswith("- "): return True
    return False


def parse_tc_boundaries(flow_path):
    with open(flow_path, "r") as f:
        lines = f.readlines()

    body_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            body_start = i + 1
            break

    tc_markers = []
    seen_markers = set()

    for i in range(body_start, len(lines)):
        line = lines[i]
        stripped = line.strip()

        if BORDER_RE.match(stripped):
            continue

        matched = None
        m = TC_MARKER_RE.match(stripped)
        if m and "═" in stripped:
            matched = (m.group(1).strip(), m.group(2).strip())

        if not matched:
            for pattern in (TC_BETWEEN_BORDERS_PUNCT_RE, TC_BETWEEN_BORDERS_RE):
                m2 = pattern.match(stripped)
                if m2:
                    has_border_above = False
                    for j in range(max(body_start, i - 3), i):
                        if BORDER_RE.match(lines[j].strip()):
                            has_border_above = True
                            break
                    has_border_below = False
                    for j in range(i + 1, min(len(lines), i + 4)):
                        if BORDER_RE.match(lines[j].strip()):
                            has_border_below = True
                            break
                    if has_border_above:
                        matched = (m2.group(1).strip(), m2.group(2).strip())
                        break

        if matched:
            tc_id, tc_desc = matched
            dedup_key = (tc_id, tc_desc[:30])
            if dedup_key not in seen_markers:
                seen_markers.add(dedup_key)
                tc_markers.append((i, tc_id, tc_desc))

    if not tc_markers:
        return []

    cmd_index = 0
    tc_start_cmds = {}
    marker_ptr = 0
    next_marker_line = tc_markers[0][0] if tc_markers else None
    cmd_positions = []
    tc_command_starts = []
    cmd_idx = 0
    current_marker = 0
    cmd_line_numbers = []

    for i, line in enumerate(lines):
        if current_marker < len(tc_markers) and i == tc_markers[current_marker][0]:
            tc_command_starts.append(None)
            current_marker += 1
        if is_top_level_command(line):
            cmd_line_numbers.append(i + 1)
            for m_idx in range(len(tc_command_starts)):
                if tc_command_starts[m_idx] is None:
                    tc_command_starts[m_idx] = cmd_idx
            cmd_idx += 1

    total_commands = cmd_idx
    for m_idx in range(len(tc_command_starts)):
        if tc_command_starts[m_idx] is None:
            tc_command_starts[m_idx] = total_commands

    tcs = []
    for i, (line_idx, tc_id, tc_desc) in enumerate(tc_markers):
        start = tc_command_starts[i]
        if i + 1 < len(tc_markers):
            end = tc_command_starts[i + 1] - 1
        else:
            end = total_commands - 1

        tc_cmd_lines = []
        for ci in range(start, end + 1):
            if ci < len(cmd_line_numbers):
                tc_cmd_lines.append(cmd_line_numbers[ci])

        tcs.append({
            "id": tc_id,
            "name": f"{tc_id}: {tc_desc}",
            "start_cmd": start,
            "end_cmd": end,
            "line_number": line_idx + 1,
            "cmd_line_numbers": tc_cmd_lines,
        })

    for i in range(len(tcs) - 1):
        if tcs[i]["start_cmd"] > tcs[i]["end_cmd"]:
            next_tc_id = tcs[i + 1]["id"]
            my_start = tcs[i + 1]["start_cmd"]
            my_end = tcs[i + 1]["end_cmd"]
            scan_cmd = my_start
            split_at = None
            tc_marker_line = tc_markers[i][0] if i < len(tc_markers) else 0
            next_tc_marker_line = tc_markers[i + 1][0] if i + 1 < len(tc_markers) else len(lines)
            for li in range(next_tc_marker_line + 1, len(lines)):
                stripped = lines[li].strip()
                if stripped.startswith("#") and next_tc_id in stripped:
                    split_at = scan_cmd
                    break
                if is_top_level_command(lines[li]):
                    scan_cmd += 1

            if split_at is not None and split_at > my_start:
                tcs[i]["start_cmd"] = my_start
                tcs[i]["end_cmd"] = split_at - 1
                tcs[i + 1]["start_cmd"] = split_at
            elif my_end > my_start:
                mid = my_start + (my_end - my_start + 1) // 2
                tcs[i]["start_cmd"] = my_start
                tcs[i]["end_cmd"] = mid - 1
                tcs[i + 1]["start_cmd"] = mid

    first_tc_start = tc_command_starts[0] if tc_command_starts else 0
    if first_tc_start > 0:
        setup_cmd_lines = [cmd_line_numbers[ci] for ci in range(first_tc_start) if ci < len(cmd_line_numbers)]
        tcs.insert(0, {
            "id": "Setup",
            "name": "Setup (login, navigation, config loading)",
            "start_cmd": 0,
            "end_cmd": first_tc_start - 1,
            "line_number": setup_cmd_lines[0] if setup_cmd_lines else 1,
            "cmd_line_numbers": setup_cmd_lines,
        })

    return tcs


# ─── Flow Path Resolution ───────────────────────────────────────────────────


def resolve_flow_from_data_file(data_file_path):
    with open(data_file_path, "r") as f:
        content = f.read()
    matches = re.findall(r"runFlow:\s*(.*flows/\S+\.yaml)", content)
    if not matches:
        return None
    flow_relative = matches[-1].strip()
    data_dir = os.path.dirname(os.path.abspath(data_file_path))
    flow_path = os.path.normpath(os.path.join(data_dir, flow_relative))
    return flow_path if os.path.exists(flow_path) else None


def get_flow_name(data_file_path):
    return Path(data_file_path).stem


# ─── Command Description Helper ────────────────────────────────────────────


_CMD_LABELS = {
    "tapOnElement": "Tap",
    "assertConditionCommand": "Assert",
    "waitForAnimationToEndCommand": "Wait for animation",
    "scrollUntilVisible": "Scroll until visible",
    "swipeCommand": "Swipe",
    "runFlowCommand": "Run flow",
    "launchAppCommand": "Launch app",
    "applyConfigurationCommand": "Apply config",
    "defineVariablesCommand": "Define variables",
    "runScriptCommand": "Run script",
    "inputTextCommand": "Input text",
    "pressKeyCommand": "Press key",
    "backPressCommand": "Back press",
    "eraseTextCommand": "Erase text",
    "openLinkCommand": "Open link",
    "setLocationCommand": "Set location",
    "takeScreenshotCommand": "Take screenshot",
    "stopAppCommand": "Stop app",
    "clearStateCommand": "Clear state",
    "evalScriptCommand": "Eval script",
}

_INTERNAL_CMD_TYPES = {
    "defineVariablesCommand",
    "evalScriptCommand",
    "applyConfigurationCommand",
}


def describe_command(cmd_type, cmd_obj):
    label = _CMD_LABELS.get(cmd_type, cmd_type.replace("Command", "").replace("command", ""))
    inner = cmd_obj.get(cmd_type, {}) if cmd_obj else {}
    detail = ""

    if cmd_type == "runFlowCommand":
        src = inner.get("sourceDescription", "")
        if src:
            detail = Path(src).name
        else:
            cond = inner.get("condition", {})
            vis = cond.get("visible", {})
            nvis = cond.get("notVisible", {})
            target = vis or nvis
            text = target.get("textRegex") or target.get("text") or target.get("id") or ""
            if nvis and text: detail = f"when not visible: {text}"
            elif text: detail = f"when visible: {text}"
    elif cmd_type in ("tapOnElement",):
        sel = inner.get("selector", {})
        text = sel.get("textRegex") or sel.get("text") or sel.get("id") or ""
        if not text and sel.get("containsChild"):
            child = sel["containsChild"]
            text = f"containsChild: {child.get('idRegex') or child.get('id') or child.get('textRegex') or ''}"
        detail = text
    elif cmd_type == "assertConditionCommand":
        cond = inner.get("condition", {})
        vis = cond.get("visible", {})
        nvis = cond.get("notVisible", {})
        target = vis or nvis
        text = target.get("textRegex") or target.get("text") or target.get("id") or ""
        if nvis and text: detail = f"not visible: {text}"
        else: detail = text
    elif cmd_type == "swipeCommand":
        detail = inner.get("direction", "")
    elif cmd_type == "scrollUntilVisible":
        el = inner.get("element", {})
        detail = el.get("textRegex") or el.get("text") or el.get("id") or ""
        direction = inner.get("direction", "")
        if direction and detail: detail = f"{detail} ({direction})"
    elif cmd_type == "waitForAnimationToEndCommand":
        timeout = inner.get("timeout", "")
        if timeout: detail = f"timeout: {timeout}ms"
    elif cmd_type == "launchAppCommand":
        app_id = inner.get("appId", "")
        clear = inner.get("clearState", False)
        detail = app_id
        if clear: detail += " (clear state)"
    elif cmd_type == "inputTextCommand":
        detail = inner.get("text", "")
    elif cmd_type == "openLinkCommand":
        detail = inner.get("link", "")

    if len(detail) > 80:
        detail = detail[:77] + "..."

    return label, detail


# ─── Commands.json Parser ───────────────────────────────────────────────────


def parse_commands_json(commands_json_path, flow_name=None):
    """
    Parse Maestro's commands.json artifact. (Updated for BrowserStack compatibility)
    """
    with open(commands_json_path, "r", errors="replace") as f:
        content = f.read().strip()

    if not content:
        raise ValueError(f"{commands_json_path} is empty.")

    try:
        # Standard JSON (local Maestro array, or BrowserStack /commandlogs array)
        raw = json.loads(content)
    except json.JSONDecodeError:
        try:
            # Fallback: JSONL (one JSON object per line)
            raw = [json.loads(line) for line in content.splitlines() if line.strip()]
        except json.JSONDecodeError:
            raise ValueError(
                f"{commands_json_path} is not valid JSON. It looks like a plain-text "
                "Maestro log (maestrologs / .log). Download the structured JSON from the "
                "BrowserStack '/commandlogs' endpoint and pass that file instead."
            )

    # Unwrap common envelope shapes: {"commands": [...]} / {"data": [...]} / single object
    if isinstance(raw, dict):
        for key in ("commands", "commandlogs", "data", "logs"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raw = [raw]
    raw = [e for e in raw if isinstance(e, dict)]

    flow_start_seq = 0
    depth0_flows = []
    for entry in raw:
        meta = entry.get("metadata") or entry
        if meta.get("depth") == 0:
            cmd_obj = entry.get("command", {})
            cmd_type = list(cmd_obj.keys())[0] if cmd_obj else ""
            if cmd_type == "runFlowCommand":
                src = cmd_obj.get("runFlowCommand", {}).get("sourceDescription", "")
                depth0_flows.append((meta.get("sequenceNumber", 0), src))

    if depth0_flows:
        if flow_name:
            for seq, src in depth0_flows:
                if flow_name.lower() in src.lower():
                    flow_start_seq = seq
                    break
            else:
                flow_start_seq = depth0_flows[-1][0]
        else:
            flow_start_seq = depth0_flows[-1][0]

    commands = []
    # Use enumerate to inject missing sequence numbers from BrowserStack logs
    for index, entry in enumerate(raw):
        meta = entry.get("metadata") or entry 
        
        seq = meta.get("sequenceNumber")
        if seq is None:
            seq = index + 1 
            
        depth = meta.get("depth", 1)

        if depth != 1:
            continue
            
        if depth0_flows and seq <= flow_start_seq:
            continue

        cmd_obj = entry.get("command", {})
        cmd_type = list(cmd_obj.keys())[0] if cmd_obj else "unknown"

        if cmd_type in ("applyConfigurationCommand",):
            continue

        status = meta.get("status", entry.get("status", "UNKNOWN")) or "UNKNOWN"
        duration = meta.get("duration", entry.get("duration", 0)) or 0
        error_msg = None

        if status in ("FAILED", "ERROR"):
            if isinstance(meta.get("error"), str):
                error_msg = meta.get("error")
            else:
                meta_error = meta.get("error") or entry.get("error")
                if isinstance(meta_error, dict):
                    error_msg = meta_error.get("message") or meta_error.get("debugMessage")
            
            if not error_msg:
                eval_cmd = meta.get("evaluatedCommand", {})
                if isinstance(eval_cmd, dict):
                    for v in eval_cmd.values():
                        if isinstance(v, dict) and "error" in v:
                            error_msg = v["error"]
                            break

        label, detail = describe_command(cmd_type, cmd_obj)
        commands.append({
            "seq": seq,
            "command_type": cmd_type,
            "command": cmd_obj,
            "label": label,
            "detail": detail,
            "status": status,
            "duration_ms": duration,
            "depth": depth,
            "error": error_msg,
        })

    commands.sort(key=lambda c: c["seq"])

    all_entries_by_seq = {}
    for idx, entry in enumerate(raw):
        m = entry.get("metadata") or entry
        s = m.get("sequenceNumber")
        if s is None:
            s = idx + 1
        all_entries_by_seq[s] = entry

    for i, cmd in enumerate(commands):
        if cmd["status"] not in ("FAILED", "ERROR"):
            continue
        child_end = commands[i + 1]["seq"] - 1 if i + 1 < len(commands) else max(all_entries_by_seq.keys()) if all_entries_by_seq else cmd["seq"]

        deepest_fail_seq = None
        deepest_fail_error = None
        deepest_fail_desc = None
        for child_seq in range(cmd["seq"] + 1, child_end + 1):
            if child_seq not in all_entries_by_seq:
                continue
            child_entry = all_entries_by_seq[child_seq]
            child_meta = child_entry.get("metadata") or child_entry
            if child_meta.get("status") in ("FAILED", "ERROR"):
                child_error = None
                ce = child_meta.get("error") or child_entry.get("error")
                if isinstance(ce, str):
                    child_error = ce
                elif isinstance(ce, dict):
                    child_error = ce.get("message") or ce.get("debugMessage")
                deepest_fail_seq = child_seq
                if child_error:
                    deepest_fail_error = child_error
                child_cmd_obj = child_entry.get("command", {})
                child_cmd_type = list(child_cmd_obj.keys())[0] if child_cmd_obj else "unknown"
                child_label, child_detail = describe_command(child_cmd_type, child_cmd_obj)
                deepest_fail_desc = f"{child_label}: {child_detail}" if child_detail else child_label

        if not cmd["error"] and deepest_fail_error:
            cmd["error"] = deepest_fail_error
        if deepest_fail_seq is not None:
            cmd["_child_screenshot_seq"] = deepest_fail_seq
        if deepest_fail_desc:
            cmd["_failed_at"] = deepest_fail_desc

    return commands


def find_screenshots_dir(flow_artifact_dir):
    screenshots_dir = os.path.join(flow_artifact_dir, "screenshots")
    if os.path.isdir(screenshots_dir):
        return screenshots_dir
    return None


def find_screenshot_for_step(flow_artifact_dir, seq_number):
    screenshots_dir = find_screenshots_dir(flow_artifact_dir)
    if not screenshots_dir:
        return None
    candidates = (seq_number + 1, seq_number) if isinstance(seq_number, int) else ()
    for candidate in candidates:
        pattern = os.path.join(screenshots_dir, f"step-{candidate:03d}-*.png")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
        pattern2 = os.path.join(screenshots_dir, f"step-{candidate}-*.png")
        matches2 = glob.glob(pattern2)
        if matches2:
            return matches2[0]

    # BrowserStack ships one failure screenshot per testcase (no step numbering)
    failure_pngs = sorted(glob.glob(os.path.join(screenshots_dir, "failure-*.png")))
    if failure_pngs:
        return failure_pngs[0]

    all_pngs = glob.glob(os.path.join(screenshots_dir, "*.png"))
    for png in all_pngs:
        basename = os.path.basename(png)
        for candidate in candidates:
            if f"-{candidate:03d}" in basename or f"-{candidate}-" in basename:
                return png
    return None


# ─── TC Status Mapper ───────────────────────────────────────────────────────


def map_commands_to_tcs(tcs, commands, flow_artifact_dir):
    results = []
    total_json_cmds = len(commands)

    if not tcs:
        return results

    total_yaml_cmds = max(tc["end_cmd"] for tc in tcs) + 1
    if total_yaml_cmds == 0:
        total_yaml_cmds = 1
    ratio = total_json_cmds / total_yaml_cmds if total_yaml_cmds > 0 else 1.0

    for tc in tcs:
        tc_cmd_lines = tc.get("cmd_line_numbers", [])
        tc_result = {
            "id": tc["id"],
            "name": tc["name"],
            "start_cmd": tc["start_cmd"],
            "end_cmd": tc["end_cmd"],
            "line_number": tc.get("line_number"),
            "status": "PASSED",
            "duration_ms": 0,
            "steps": [],
            "error": None,
            "screenshot_b64": None,
        }

        json_start_raw = int(round(tc["start_cmd"] * ratio))
        json_end_raw = int(round((tc["end_cmd"] + 1) * ratio)) - 1
        tc_never_ran = json_start_raw >= total_json_cmds
        json_start = max(0, min(json_start_raw, total_json_cmds - 1))
        json_end = max(json_start, min(json_end_raw, total_json_cmds - 1))

        if tc["start_cmd"] > tc["end_cmd"]:
            json_start = int(round(tc["start_cmd"] * ratio))
            json_end = json_start - 1
            tc_never_ran = False

        has_failed = False
        has_any_completed = False
        all_skipped = True
        step_counter = 0
        yaml_cmd_pointer = 0

        for cmd_idx in range(json_start, json_end + 1):
            if cmd_idx >= total_json_cmds:
                break
            cmd = commands[cmd_idx]
            is_internal = cmd["command_type"] in _INTERNAL_CMD_TYPES
            yaml_line = None
            if tc_cmd_lines and not is_internal:
                if yaml_cmd_pointer < len(tc_cmd_lines):
                    yaml_line = tc_cmd_lines[yaml_cmd_pointer]
                yaml_cmd_pointer += 1

            step = {
                "seq": cmd["seq"],
                "step_num": step_counter + 1,
                "yaml_line": yaml_line,
                "command": cmd["command_type"],
                "label": cmd.get("label", cmd["command_type"]),
                "detail": cmd.get("detail", ""),
                "status": cmd["status"],
                "duration_ms": cmd["duration_ms"],
                "error": cmd.get("error"),
                "failed_at": cmd.get("_failed_at"),
                "screenshot_b64": None,
            }
            step_counter += 1
            tc_result["duration_ms"] += cmd["duration_ms"]
            status_upper = cmd["status"].upper()

            if status_upper in ("COMPLETED", "SUCCESS", "PASSED"):
                has_any_completed = True
                all_skipped = False
                step["status"] = "COMPLETED"
            elif status_upper in ("FAILED", "ERROR"):
                has_failed = True
                all_skipped = False
                step["status"] = "FAILED"
                if not tc_result["error"]:
                    tc_result["error"] = cmd.get("error", "Step failed")

                screenshot_path = None
                child_seq = cmd.get("_child_screenshot_seq")
                if child_seq is not None:
                    screenshot_path = find_screenshot_for_step(flow_artifact_dir, child_seq)
                if not screenshot_path:
                    screenshot_path = find_screenshot_for_step(flow_artifact_dir, cmd["seq"])
                if screenshot_path:
                    try:
                        b64, fmt = _encode_screenshot(screenshot_path)
                        step["screenshot_b64"] = b64
                        step["screenshot_fmt"] = fmt
                        tc_result["screenshot_b64"] = b64
                        tc_result["screenshot_fmt"] = fmt
                    except (IOError, OSError):
                        pass
            else:
                step["status"] = "SKIPPED"
            tc_result["steps"].append(step)

        if tc_never_ran:
            tc_result["status"] = "SKIPPED"
            tc_result["steps"] = []
            tc_result["error"] = None
            tc_result["screenshot_b64"] = None
            tc_result["skip_reason"] = "Execution stopped after prior failure"
        elif has_failed:
            tc_result["status"] = "FAILED"
        elif all_skipped and tc_result["steps"]:
            tc_result["status"] = "SKIPPED"
            tc_result["skip_reason"] = "All steps were conditionally skipped"
        elif not tc_result["steps"]:
            tc_result["status"] = "UNKNOWN"
        else:
            tc_result["status"] = "PASSED"

        results.append(tc_result)

    seen_failure = False
    for tc_result in results:
        if tc_result["status"] == "FAILED":
            seen_failure = True
        elif tc_result["status"] == "UNKNOWN":
            if seen_failure:
                tc_result["status"] = "SKIPPED"
                tc_result["skip_reason"] = "Execution stopped after prior failure"
            else:
                tc_result["status"] = "PASSED"

    has_any_failure = any(r["status"] == "FAILED" for r in results)
    if has_any_failure and total_json_cmds < total_yaml_cmds * 0.5:
        failed_yaml_start = None
        for tc_result in results:
            if tc_result["status"] == "FAILED":
                failed_yaml_start = tc_result["start_cmd"]
                break
        if failed_yaml_start is not None:
            for tc_result in results:
                if tc_result["status"] != "PASSED" or tc_result["id"] == "Setup":
                    continue
                if tc_result["start_cmd"] >= failed_yaml_start:
                    tc_result["status"] = "SKIPPED"
                    tc_result["skip_reason"] = "Test steps not reached before failure"
                else:
                    yaml_cmd_count = tc_result["end_cmd"] - tc_result["start_cmd"] + 1
                    actual_steps = len(tc_result["steps"])
                    if yaml_cmd_count > 3 and actual_steps <= 1:
                        tc_result["status"] = "SKIPPED"
                        tc_result["skip_reason"] = "Test steps not reached before failure"
    return results


# ─── Artifact Directory Finder ───────────────────────────────────────────────


def find_flow_artifact_dir(test_output_dir, flow_name, allowed_dirs=None):
    if not os.path.isdir(test_output_dir):
        return None
    def _match_flow_in(parent_dir):
        for entry in os.listdir(parent_dir):
            entry_path = os.path.join(parent_dir, entry)
            if os.path.isdir(entry_path):
                entry_base = re.sub(r"-shard-\d+$", "", entry)
                if entry_base.lower() == flow_name.lower():
                    return entry_path
        return None
    direct = _match_flow_in(test_output_dir)
    if direct:
        return direct
    entries = sorted(
        [e for e in os.listdir(test_output_dir)
         if os.path.isdir(os.path.join(test_output_dir, e)) and (allowed_dirs is None or e in allowed_dirs)],
        reverse=True,
    )
    for timestamp_dir in entries:
        full_ts_dir = os.path.join(test_output_dir, timestamp_dir)
        match = _match_flow_in(full_ts_dir)
        if match:
            return match
        subdirs = [e for e in os.listdir(full_ts_dir) if os.path.isdir(os.path.join(full_ts_dir, e))]
        if len(subdirs) == 1:
            return os.path.join(full_ts_dir, subdirs[0])
    return None


# ─── Maestro Test Runner ────────────────────────────────────────────────────


def run_maestro_test(data_file_or_dir, test_output_dir):
    import shlex
    cmd = [
        "maestro",
        "test",
        f"--test-output-dir={test_output_dir}",
        data_file_or_dir,
    ]
    print(f"\n{'='*70}")
    print(f"  Running: {' '.join(cmd)}")
    print(f"{'='*70}\n")
    os.makedirs(test_output_dir, exist_ok=True)
    log_path = os.path.join(test_output_dir, "_maestro_output.log")
    cmd_str = " ".join(shlex.quote(c) for c in cmd)
    shell_cmd = f"set -o pipefail; {cmd_str} 2>&1 | tee {shlex.quote(log_path)}"
    result = subprocess.run(shell_cmd, shell=True, executable="/bin/bash")
    maestro_log = ""
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            maestro_log = f.read()
    return result.returncode, maestro_log


# ─── HTML Report Generator ──────────────────────────────────────────────────


def format_duration(ms):
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


def generate_html_report(flow_results, output_path, total_duration_ms=0, platform_name="Unknown"):
    total_tcs = passed_tcs = failed_tcs = skipped_tcs = 0
    total_steps = passed_steps = failed_steps = skipped_steps = 0

    for flow in flow_results:
        for tc in flow["tcs"]:
            if tc["id"] == "Setup":
                for step in tc["steps"]:
                    total_steps += 1
                    if step["status"] == "COMPLETED": passed_steps += 1
                    elif step["status"] == "FAILED": failed_steps += 1
                    else: skipped_steps += 1
                continue

            total_tcs += 1
            if tc["status"] == "PASSED": passed_tcs += 1
            elif tc["status"] == "FAILED": failed_tcs += 1
            else: skipped_tcs += 1

            for step in tc["steps"]:
                total_steps += 1
                if step["status"] == "COMPLETED": passed_steps += 1
                elif step["status"] == "FAILED": failed_steps += 1
                else: skipped_steps += 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    platform_lower = platform_name.lower()
    if "android" in platform_lower and "ios" in platform_lower: platform_css_class, platform_icon = "multi", "📱"
    elif "android" in platform_lower: platform_css_class, platform_icon = "android", "🤖"
    elif "ios" in platform_lower: platform_css_class, platform_icon = "ios", "🍎"
    else: platform_css_class, platform_icon = "unknown", "❓"

    flow_sections = []
    for f_idx, flow in enumerate(flow_results):
        flow_tcs_html = []
        flow_tc_count = flow_tc_passed = flow_tc_failed = flow_tc_skipped = 0

        for tc in flow["tcs"]:
            is_setup = tc["id"] == "Setup"
            if not is_setup:
                flow_tc_count += 1
                if tc["status"] == "PASSED": flow_tc_passed += 1
                elif tc["status"] == "FAILED": flow_tc_failed += 1
                else: flow_tc_skipped += 1

            status_class = {"PASSED": "passed", "FAILED": "failed", "SKIPPED": "skipped"}.get(tc["status"], "skipped")
            status_icon = {"PASSED": "✅", "FAILED": "❌", "SKIPPED": "⏭️"}.get(tc["status"], "❓")
            steps_html = []
            
            for step in tc["steps"]:
                if step["status"] == "SKIPPED": continue
                step_status_class = {"COMPLETED": "passed", "FAILED": "failed", "SKIPPED": "skipped"}.get(step["status"], "skipped")
                step_icon = {"COMPLETED": "✅", "FAILED": "❌", "SKIPPED": "⏭️"}.get(step["status"], "⏭️")

                error_html = ""
                if step.get("failed_at") or step.get("error"):
                    parts = []
                    if step.get("failed_at"):
                        fa_escaped = step["failed_at"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        parts.append(f'<div class="failed-at">Failed at: <strong>{fa_escaped}</strong></div>')
                    if step.get("error"):
                        error_escaped = step["error"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                        parts.append(f'<div class="step-error">{error_escaped}</div>')
                    error_html = "\n".join(parts)

                screenshot_html = ""
                if step.get("screenshot_b64"):
                    screenshot_html = f"""<div class="screenshot-container"><img src="data:image/{step.get('screenshot_fmt', 'png')};base64,{step['screenshot_b64']}" alt="Failure screenshot" class="screenshot" loading="lazy" onclick="this.classList.toggle('expanded')" /><span class="screenshot-hint">Click to expand</span></div>"""

                step_label = step.get("label", step["command"])
                step_detail = step.get("detail", "")
                if step_detail:
                    detail_escaped = step_detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    command_cell = f'<strong>{step_label}</strong> <span class="step-detail">{detail_escaped}</span>'
                else:
                    command_cell = f'<strong>{step_label}</strong>'

                yaml_line_cell = step.get('yaml_line', '')
                yaml_line_display = f'L{yaml_line_cell}' if yaml_line_cell else ''

                steps_html.append(f"""
                    <tr class="step-row {step_status_class}">
                        <td class="step-num">{step.get('step_num', step['seq'])}</td>
                        <td class="step-line">{yaml_line_display}</td>
                        <td class="step-icon">{step_icon}</td>
                        <td class="step-command">{command_cell}</td>
                        <td class="step-status">{step['status']}</td>
                        <td class="step-duration">{format_duration(step['duration_ms'])}</td>
                    </tr>""")
                if error_html or screenshot_html:
                    steps_html.append(f'<tr class="step-detail-row {step_status_class}"><td colspan="5">{error_html}{screenshot_html}</td></tr>')

            tc_id = f"flow-{f_idx}-tc-{tc['id'].replace(' ', '-').replace('&', 'and')}"
            skip_reason_html = f'<div class="skip-reason">⏭️ {tc["skip_reason"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</div>' if tc.get("skip_reason") else ""
            tc_error_html = f'<div class="tc-crash-error"><strong>Maestro crashed — last output:</strong><pre>{tc["error"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</pre></div>' if tc.get("error") and not tc.get("steps") else ""

            flow_tcs_html.append(f"""
                <div class="tc-card {status_class}">
                    <div class="tc-header" onclick="toggleTC('{tc_id}')">
                        <span class="tc-icon">{status_icon}</span>
                        <span class="tc-name">{tc['name']}</span>
                        <span class="tc-line-badge">L{tc.get('line_number', '')}</span>
                        <span class="tc-duration">{format_duration(tc['duration_ms'])}</span>
                        <span class="tc-toggle" id="{tc_id}-toggle">▶</span>
                    </div>
                    <div class="tc-steps" id="{tc_id}" style="display:none;">
                        {skip_reason_html}{tc_error_html}
                        <table class="steps-table">
                            <thead><tr><th>Step</th><th>Line</th><th></th><th>Command</th><th>Status</th><th>Duration</th></tr></thead>
                            <tbody>{''.join(steps_html)}</tbody>
                        </table>
                    </div>
                </div>""")

        flow_status_class = "passed" if flow_tc_failed == 0 and flow_tc_skipped == 0 else ("failed" if flow_tc_failed > 0 else "skipped")
        flow_status_text = "PASSED" if flow_tc_failed == 0 and flow_tc_skipped == 0 else ("FAILED" if flow_tc_failed > 0 else "PARTIAL")

        tc_summary_rows = []
        for tc in flow["tcs"]:
            if tc["id"] == "Setup": continue
            s_class = {"PASSED": "passed", "FAILED": "failed", "SKIPPED": "skipped"}.get(tc["status"], "skipped")
            s_icon = {"PASSED": "✅", "FAILED": "❌", "SKIPPED": "⏭️"}.get(tc["status"], "❓")
            tc_name_escaped = tc["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            skip_note = f' <span class="tc-summary-skip-note">({tc["skip_reason"]})</span>' if tc.get("skip_reason") else ""
            tc_summary_rows.append(f'<tr class="tc-summary-row {s_class}"><td>{s_icon}</td><td class="tc-summary-id">{tc["id"]}</td><td class="tc-summary-name">{tc_name_escaped}{skip_note}</td><td class="tc-summary-steps">{len(tc["steps"])} steps</td><td class="tc-summary-duration">{format_duration(tc["duration_ms"])}</td><td class="tc-summary-status {s_class}">{tc["status"]}</td></tr>')
        
        tc_summary_html = f'<table class="tc-summary-table"><thead><tr><th></th><th>ID</th><th>Test Case</th><th>Steps</th><th>Duration</th><th>Status</th></tr></thead><tbody>{"".join(tc_summary_rows)}</tbody></table>' if tc_summary_rows else ""

        flow_sections.append(f"""
            <div class="flow-card">
                <div class="flow-header {flow_status_class}" onclick="this.parentElement.classList.toggle('collapsed')">
                    <div class="flow-header-top"><h2>{flow['flow_name']}</h2><span class="flow-chevron">▼</span></div>
                    <div class="flow-meta"><span class="flow-status-badge {flow_status_class}">{flow_status_text}</span><span class="flow-stats">{flow_tc_passed}/{flow_tc_count} passed{f' · {flow_tc_failed} failed' if flow_tc_failed else ''}{f' · {flow_tc_skipped} skipped' if flow_tc_skipped else ''}</span><span class="flow-duration">{format_duration(flow['total_duration_ms'])}</span></div>
                    <div class="flow-file">{flow['data_file']}</div>
                </div>
                <div class="flow-body">{tc_summary_html}<div class="flow-tcs">{''.join(flow_tcs_html)}</div></div>
            </div>""")

    overall_status = "PASSED" if failed_tcs == 0 and skipped_tcs == 0 else "FAILED"
    overall_class = "passed" if overall_status == "PASSED" else "failed"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Maestro Test Report</title>
    <style>
        :root {{ --bg: #f8f9fa; --card-bg: #ffffff; --text: #1a1a2e; --text-muted: #6c757d; --border: #e0e0e0; --passed: #28a745; --passed-bg: #d4edda; --passed-border: #c3e6cb; --failed: #dc3545; --failed-bg: #f8d7da; --failed-border: #f5c6cb; --skipped: #ffc107; --skipped-bg: #fff3cd; --skipped-border: #ffeeba; --header-bg: #1a1a2e; --header-text: #ffffff; --shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
        .header {{ background: var(--header-bg); color: var(--header-text); padding: 24px 32px; }}
        .header h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 4px; }}
        .header .timestamp {{ font-size: 13px; opacity: 0.7; }}
        .header .platform-badge {{ display: inline-block; margin-top: 8px; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
        .header .platform-badge.android {{ background: rgba(61, 220, 132, 0.2); color: #3ddc84; }}
        .header .platform-badge.ios {{ background: rgba(0, 122, 255, 0.2); color: #56a8ff; }}
        .header .platform-badge.multi {{ background: rgba(255, 255, 255, 0.15); color: #ffffff; }}
        .header .platform-badge.unknown {{ background: rgba(255, 255, 255, 0.1); color: rgba(255, 255, 255, 0.6); }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; padding: 24px 32px; max-width: 900px; }}
        .summary-card {{ background: var(--card-bg); border-radius: 8px; padding: 16px 20px; text-align: center; box-shadow: var(--shadow); border-top: 3px solid var(--border); }}
        .summary-card.overall {{ border-top-color: var(--{overall_class}); }}
        .summary-card.pass {{ border-top-color: var(--passed); }}
        .summary-card.fail {{ border-top-color: var(--failed); }}
        .summary-card.skip {{ border-top-color: var(--skipped); }}
        .summary-card .number {{ font-size: 32px; font-weight: 700; }}
        .summary-card.overall .number {{ color: var(--{overall_class}); }}
        .summary-card.pass .number {{ color: var(--passed); }}
        .summary-card.fail .number {{ color: var(--failed); }}
        .summary-card.skip .number {{ color: var(--skipped); }}
        .summary-card .label {{ font-size: 12px; text-transform: uppercase; color: var(--text-muted); margin-top: 4px; }}
        .step-summary {{ padding: 8px 32px; font-size: 12px; color: var(--text-muted); display: flex; gap: 16px; }}
        .content {{ padding: 0 32px 32px; max-width: 1100px; }}
        .flow-card {{ background: var(--card-bg); border-radius: 8px; box-shadow: var(--shadow); margin-bottom: 24px; overflow: hidden; }}
        .flow-header {{ padding: 16px 20px; border-left: 4px solid var(--border); cursor: pointer; user-select: none; }}
        .flow-header.passed {{ border-left-color: var(--passed); }}
        .flow-header.failed {{ border-left-color: var(--failed); }}
        .flow-header.skipped {{ border-left-color: var(--skipped); }}
        .flow-header-top {{ display: flex; align-items: center; justify-content: space-between; }}
        .flow-chevron {{ font-size: 14px; color: var(--text-muted); transition: transform 0.2s; }}
        .flow-card.collapsed .flow-chevron {{ transform: rotate(-90deg); }}
        .flow-card.collapsed .flow-body {{ display: none; }}
        .flow-header h2 {{ font-size: 18px; font-weight: 600; margin-bottom: 6px; }}
        .flow-meta {{ display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }}
        .flow-status-badge {{ font-size: 11px; font-weight: 700; text-transform: uppercase; padding: 2px 8px; border-radius: 4px; }}
        .flow-status-badge.passed {{ background: var(--passed-bg); color: var(--passed); }}
        .flow-status-badge.failed {{ background: var(--failed-bg); color: var(--failed); }}
        .flow-status-badge.skipped {{ background: var(--skipped-bg); color: var(--skipped); }}
        .flow-stats, .flow-duration, .flow-file {{ font-size: 13px; color: var(--text-muted); }}
        .flow-tcs {{ padding: 8px 16px 16px; }}
        .tc-card {{ border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px; overflow: hidden; }}
        .tc-card.passed {{ border-color: var(--passed-border); }}
        .tc-card.failed {{ border-color: var(--failed-border); }}
        .tc-card.skipped {{ border-color: var(--skipped-border); }}
        .tc-header {{ display: flex; align-items: center; padding: 10px 14px; cursor: pointer; gap: 8px; }}
        .tc-card.passed .tc-header {{ background: var(--passed-bg); }}
        .tc-card.failed .tc-header {{ background: var(--failed-bg); }}
        .tc-card.skipped .tc-header {{ background: var(--skipped-bg); }}
        .tc-icon {{ font-size: 16px; flex-shrink: 0; }}
        .tc-name {{ flex: 1; font-size: 14px; font-weight: 500; }}
        .tc-duration {{ font-size: 12px; color: var(--text-muted); margin-right: 8px; }}
        .tc-line-badge {{ font-size: 11px; font-family: monospace; color: var(--text-muted); background: var(--border); padding: 1px 6px; border-radius: 3px; margin-right: 8px; }}
        .tc-toggle {{ font-size: 12px; color: var(--text-muted); transition: transform 0.2s; }}
        .tc-toggle.open {{ transform: rotate(90deg); }}
        .tc-steps {{ border-top: 1px solid var(--border); }}
        .steps-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        .steps-table th {{ text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; color: var(--text-muted); border-bottom: 1px solid var(--border); }}
        .steps-table td {{ padding: 6px 12px; border-bottom: 1px solid #f0f0f0; }}
        .step-row.failed td {{ background: #fff5f5; }}
        .step-row.skipped td {{ opacity: 0.6; }}
        .step-detail-row td {{ padding: 8px 12px 12px; }}
        .step-error {{ background: var(--failed-bg); border: 1px solid var(--failed-border); border-radius: 4px; padding: 8px 12px; font-family: monospace; font-size: 12px; color: var(--failed); word-break: break-word; }}
        .failed-at {{ background: #fff0f0; border: 1px solid var(--failed-border); border-radius: 4px; padding: 8px 12px; font-size: 13px; color: #721c24; margin-bottom: 6px; }}
        .screenshot {{ max-width: 300px; border-radius: 6px; border: 1px solid var(--border); cursor: pointer; transition: max-width 0.3s; margin-top: 8px; }}
        .screenshot.expanded {{ max-width: 100%; }}
        .tc-summary-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 12px; background: var(--card-bg); }}
        .tc-summary-table th {{ text-align: left; padding: 8px 10px; font-size: 11px; text-transform: uppercase; color: var(--text-muted); border-bottom: 2px solid var(--border); }}
        .tc-summary-table td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); }}
        .tc-summary-status.passed {{ color: var(--passed); }}
        .tc-summary-status.failed {{ color: var(--failed); }}
        .tc-summary-status.skipped {{ color: var(--skipped); }}
        .tc-summary-row.failed {{ background: rgba(239, 68, 68, 0.05); }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Maestro Test Report</h1>
        <div class="timestamp">{now}</div>
        <div class="platform-badge {platform_css_class}">{platform_icon} {platform_name}</div>
    </div>
    <div class="summary">
        <div class="summary-card overall"><div class="number">{passed_tcs}/{total_tcs}</div><div class="label">Test Cases Passed</div></div>
        <div class="summary-card pass"><div class="number">{passed_tcs}</div><div class="label">Passed</div></div>
        <div class="summary-card fail"><div class="number">{failed_tcs}</div><div class="label">Failed</div></div>
        <div class="summary-card skip"><div class="number">{skipped_tcs}</div><div class="label">Skipped</div></div>
    </div>
    <div class="step-summary"><span>Steps: {total_steps} total</span><span>✅ {passed_steps} passed</span><span>❌ {failed_steps} failed</span><span>⏭️ {skipped_steps} skipped</span></div>
    <div class="content">{''.join(flow_sections)}</div>
    <script>
        function toggleTC(id) {{
            const el = document.getElementById(id);
            const toggle = document.getElementById(id + '-toggle');
            if (el.style.display === 'none') {{ el.style.display = 'block'; toggle.classList.add('open'); }}
            else {{ el.style.display = 'none'; toggle.classList.remove('open'); }}
        }}
        document.addEventListener('DOMContentLoaded', function() {{
            document.querySelectorAll('.tc-card.failed .tc-header').forEach(function(header) {{ header.click(); }});
        }});
    </script>
</body>
</html>"""

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    print(f"\n📊 Report generated: {output_path}")
    print(f"   Test Cases: {passed_tcs}/{total_tcs} passed | {failed_tcs} failed | {skipped_tcs} skipped")


# ─── Main ────────────────────────────────────────────────────────────────────


def collect_data_files(path):
    path = os.path.abspath(path)
    if os.path.isfile(path): return [path]
    elif os.path.isdir(path): return sorted(glob.glob(os.path.join(path, "*.yaml")))
    else:
        print(f"Error: {path} is not a file or directory")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Maestro Test Report Generator")
    parser.add_argument("target", nargs="+", help="Data file(s) (.yaml) or directory")
    parser.add_argument("--output", "-o", default="reports/report.html")
    parser.add_argument("--report-only", action="store_true", help="Parse existing --test-output-dir results")
    parser.add_argument("--parse-only", action="store_true", help="Only parse TC boundaries")
    parser.add_argument("--test-output-dir", default=None, help="Directory for artifacts")
    parser.add_argument("--commands-json", default=None,
                        help="Path to a commands JSON (e.g. BrowserStack /commandlogs download). "
                             "Skips running maestro; implies --report-only.")
    parser.add_argument("--screenshots-dir", default=None,
                        help="Directory containing step screenshots (parent of 'screenshots/')")

    args = parser.parse_args()
    data_files = []
    for t in args.target: data_files.extend(collect_data_files(t))
    if not data_files: sys.exit(1)

    if args.parse_only:
        for data_file in data_files:
            flow_name = get_flow_name(data_file)
            self_tcs = parse_tc_boundaries(data_file)
            flow_path = os.path.abspath(data_file) if any(tc["id"] != "Setup" for tc in self_tcs) else resolve_flow_from_data_file(data_file)
            print(f"\n  Flow: {flow_name}")
            if flow_path:
                tcs = parse_tc_boundaries(flow_path)
                real_tcs = [t for t in tcs if t["id"] != "Setup"]
                for tc in real_tcs: print(f"    • {tc['id']}: {tc['name'][:70]}")
        sys.exit(0)

    test_output_dir = args.test_output_dir or os.path.join(os.path.dirname(os.path.abspath(args.output)), ".maestro-artifacts")
    os.makedirs(test_output_dir, exist_ok=True)
    flow_results = []

    for data_file in data_files:
        flow_name = get_flow_name(data_file)
        print(f"\n  Flow: {flow_name}")

        self_tcs = parse_tc_boundaries(data_file)
        flow_path = os.path.abspath(data_file) if any(tc["id"] != "Setup" for tc in self_tcs) else resolve_flow_from_data_file(data_file)

        current_run_dirs = None
        maestro_log = ""
        if args.commands_json:
            args.report_only = True
        if not args.report_only:
            dirs_before = set(e for e in os.listdir(test_output_dir) if os.path.isdir(os.path.join(test_output_dir, e))) if os.path.isdir(test_output_dir) else set()
            exit_code, maestro_log = run_maestro_test(data_file, test_output_dir)
            dirs_after = set(e for e in os.listdir(test_output_dir) if os.path.isdir(os.path.join(test_output_dir, e))) if os.path.isdir(test_output_dir) else set()
            current_run_dirs = dirs_after - dirs_before

        tcs = parse_tc_boundaries(flow_path) if flow_path else []
        flow_artifact_dir = args.screenshots_dir or find_flow_artifact_dir(test_output_dir, flow_name, allowed_dirs=current_run_dirs)
        commands, total_duration_ms = [], 0

        if args.commands_json:
            try:
                commands = parse_commands_json(args.commands_json, flow_name=flow_name)
            except (ValueError, OSError) as e:
                print(f"Error: {e}")
                sys.exit(1)
            total_duration_ms = sum(c["duration_ms"] for c in commands)
        elif flow_artifact_dir:
            commands_json_path = os.path.join(flow_artifact_dir, "commands.json")
            if os.path.exists(commands_json_path):
                commands = parse_commands_json(commands_json_path, flow_name=flow_name)
                total_duration_ms = sum(c["duration_ms"] for c in commands)
        
        crash_error = None
        if maestro_log and not commands:
            log_lines = [l.strip() for l in maestro_log.strip().splitlines() if l.strip()]
            crash_error = "\n".join(log_lines[-20:]) if log_lines else "Maestro exited with no output"

        if tcs and commands:
            tc_results = map_commands_to_tcs(tcs, commands, flow_artifact_dir or "")
        elif tcs:
            tc_results = [{**tc, "status": "FAILED" if i == 0 and crash_error else "SKIPPED", "duration_ms": 0, "steps": [], "error": crash_error if i == 0 else None, "screenshot_b64": None} for i, tc in enumerate(tcs)]
        else:
            if commands:
                all_steps, has_failure, first_error, screenshot_b64 = [], False, None, None
                for cmd in commands:
                    step = {"seq": cmd["seq"], "command": cmd.get("command_type", cmd.get("command", "unknown")), "label": cmd.get("label", "unknown"), "detail": cmd.get("detail", ""), "status": "COMPLETED" if cmd["status"].upper() in ("COMPLETED", "SUCCESS", "PASSED") else ("FAILED" if cmd["status"].upper() in ("FAILED", "ERROR") else "SKIPPED"), "duration_ms": cmd["duration_ms"], "error": cmd.get("error"), "screenshot_b64": None}
                    if step["status"] == "FAILED" and not has_failure:
                        has_failure, first_error = True, cmd.get("error")
                        sp = find_screenshot_for_step(flow_artifact_dir or "", cmd.get("_child_screenshot_seq")) or find_screenshot_for_step(flow_artifact_dir or "", cmd["seq"])
                        if sp:
                            try:
                                b64, fmt = _encode_screenshot(sp)
                                step["screenshot_b64"], step["screenshot_fmt"], screenshot_b64 = b64, fmt, b64
                            except (IOError, OSError): pass
                    all_steps.append(step)
                tc_results = [{"id": "Full Flow", "name": f"{flow_name} (no TC markers)", "start_cmd": 0, "end_cmd": len(commands) - 1, "status": "FAILED" if has_failure else "PASSED", "duration_ms": total_duration_ms, "steps": all_steps, "error": first_error, "screenshot_b64": screenshot_b64}]
            else: tc_results = []

        flow_results.append({"flow_name": flow_name, "data_file": data_file, "overall_status": "FAILED" if any(tc["status"] == "FAILED" for tc in tc_results) else "PASSED", "total_duration_ms": total_duration_ms, "tcs": tc_results})

    generate_html_report(flow_results, args.output, platform_name=detect_platform())

if __name__ == "__main__":
    main()