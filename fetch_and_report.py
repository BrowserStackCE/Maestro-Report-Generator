#!/usr/bin/env python3
"""
BrowserStack Maestro -> HTML report pipeline.

Steps (mirrors the customer's curl workflow):
  1. GET /app-automate/builds.json                      -> find build hashed_id
  2. GET /app-automate/maestro/v2/builds/<build>        -> sessions
  3. GET /maestro/v2/builds/<build>/sessions/<sid>.json -> testcases.data[].testcases[]
  4. GET <testcase.maestro_commands>  (…/commandlogs)   -> structured JSON
     (NOT maestrologs = plain text, NOT commands_log = 404 'commandslogs')
  5. generate_report_2.py --commands-json <file> <flow.yaml>

Credentials are read from the environment only:
  BROWSERSTACK_USERNAME / BROWSERSTACK_ACCESS_KEY

Usage:
  python3 fetch_and_report.py [--build <hashed_id>] [--flows-dir <your flows folder>] [--yaml examples/Login.yaml] [--out-dir reports]
"""

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

API = "https://api-cloud.browserstack.com/app-automate"


def _load_dotenv(path=".env"):
    """Minimal .env loader (no dependency); .env values override inherited shell vars."""
    for candidate in (path, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if v:
                        os.environ[k.strip()] = v
            return


_load_dotenv()


def _auth_header():
    user = os.environ.get("BROWSERSTACK_USERNAME")
    key = os.environ.get("BROWSERSTACK_ACCESS_KEY")
    if not user or not key:
        sys.exit("Set BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY in the environment.")
    token = base64.b64encode(f"{user}:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(url, as_json=True):
    req = urllib.request.Request(url, headers=_auth_header())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}")
    return json.loads(body) if as_json else body


def find_maestro_build(limit):
    """Return the most recent build that the Maestro v2 API recognises."""
    builds = _get(f"{API}/builds.json?limit={limit}")
    for b in builds:
        bid = b["automation_build"]["hashed_id"]
        try:
            data = _get(f"{API}/maestro/v2/builds/{bid}")
        except RuntimeError:
            continue  # not a Maestro build (Espresso/XCUITest/etc.)
        if data.get("framework", "maestro") == "maestro":
            return bid, data
    sys.exit(f"No Maestro build found in the last {limit} builds.")


def iter_sessions(build):
    for dev in build.get("devices", []):
        for s in dev.get("sessions", []):
            yield s


def iter_testcases(session_detail):
    for grp in session_detail.get("testcases", {}).get("data", []):
        for tc in grp.get("testcases", []):
            yield tc


def safe(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_")[:80]


def find_flow_yaml(flows_dir, testcase_name):
    """Map a testcase name like 'data/revzilla/Login' to a YAML under flows_dir.

    Tries, in order: the full relative path (data/revzilla/Login.yaml), then any
    file whose path ends with the longest possible suffix of the name
    (revzilla/Login.yaml, then Login.yaml). Returns None when nothing matches
    or when the match is ambiguous.
    """
    if not flows_dir:
        return None
    parts = [p for p in testcase_name.replace("\\", "/").split("/") if p]
    if not parts:
        return None
    files = []
    for root, _dirs, names in os.walk(flows_dir):
        for n in names:
            if n.lower().endswith((".yaml", ".yml")):
                files.append(os.path.join(root, n))
    for take in range(len(parts), 0, -1):
        suffix = "/".join(parts[-take:]).lower()
        hits = []
        for path in files:
            rel = os.path.splitext(path.replace("\\", "/"))[0].lower()
            if rel.endswith("/" + suffix) or rel == suffix:
                hits.append(path)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # Ambiguous (e.g. several brands each have a Login.yaml): never guess,
            # a wrong flow would put another brand's TC markers on this report.
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", help="40-char build hashed_id (default: latest Maestro build)")
    ap.add_argument("--limit", type=int, default=100, help="builds to scan when --build is omitted")
    ap.add_argument("--yaml", default=None, help="Test YAML passed to generate_report_2.py (optional)")
    ap.add_argument("--flows-dir", default=None,
                    help="Folder containing your Maestro YAML files; each testcase is matched "
                         "to its YAML by name (e.g. data/revzilla/Login -> .../Login.yaml)")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--only-failed", action="store_true", help="only report non-passed testcases")
    args = ap.parse_args()

    if args.build:
        bid = args.build
        build = _get(f"{API}/maestro/v2/builds/{bid}")
    else:
        bid, build = find_maestro_build(args.limit)
    print(f"Build: {bid} status={build.get('status')}")

    here = os.path.dirname(os.path.abspath(__file__))
    report_script = os.path.join(here, "generate_report_2.py")
    os.makedirs(args.out_dir, exist_ok=True)
    generated = []

    for s in iter_sessions(build):
        sid = s["id"]
        detail = _get(f"{API}/maestro/v2/builds/{bid}/sessions/{sid}.json")
        for tc in iter_testcases(detail):
            if args.only_failed and tc.get("status") == "passed":
                continue
            url = tc.get("maestro_commands")  # ends with /commandlogs
            if not url:
                print(f"  skip {tc.get('name')}: no commandlogs URL")
                continue
            try:
                raw = _get(url, as_json=False)
                json.loads(raw)  # guardrail: must be JSON, not plain text
            except (RuntimeError, ValueError) as e:
                print(f"  skip {tc.get('name')}: {e}")
                continue

            # Testcase names repeat across sessions/shards: suffix with the unique id
            stem = f"{safe(tc['name'])}_{tc.get('id', sid)[:8]}"
            cmd_path = os.path.join(args.out_dir, f"{stem}.commands.json")
            with open(cmd_path, "wb") as f:
                f.write(raw)

            # Test YAML: matched from --flows-dir by testcase name, else --yaml, else a stub
            yaml_path = find_flow_yaml(args.flows_dir, tc["name"]) or args.yaml
            if args.flows_dir and yaml_path == args.yaml:
                print(f"  note: no YAML in {args.flows_dir} matches '{tc['name']}'")
            if not yaml_path:
                yaml_path = os.path.join(args.out_dir, f"{stem}.yaml")
                with open(yaml_path, "w") as f:
                    f.write("appId: stub\n---\n")

            # Failure screenshots: the screenshots URL returns a ZIP of PNGs
            shots_root = os.path.join(args.out_dir, f"{stem}.artifacts")
            shots_dir = os.path.join(shots_root, "screenshots")
            shots_url = tc.get("screenshots")
            if shots_url:
                try:
                    z = zipfile.ZipFile(io.BytesIO(_get(shots_url, as_json=False)))
                    os.makedirs(shots_dir, exist_ok=True)
                    pngs = [n for n in z.namelist() if n.lower().endswith(".png")]
                    for i, name in enumerate(pngs):
                        with open(os.path.join(shots_dir, f"failure-{i:03d}.png"), "wb") as out:
                            out.write(z.read(name))
                except (RuntimeError, zipfile.BadZipFile):
                    pass  # no screenshots for this testcase: report still generated

            out_html = os.path.join(args.out_dir, f"{stem}.html")
            cmd = [sys.executable, report_script, yaml_path,
                   "--commands-json", cmd_path, "-o", out_html]
            if os.path.isdir(shots_dir):
                cmd += ["--screenshots-dir", shots_root]
            print(f"  [{tc.get('status')}] {tc['name']} -> {out_html}")
            rc = subprocess.run(cmd, check=False).returncode
            if rc != 0 or not os.path.isfile(out_html):
                print(f"  !! report generation FAILED for {tc['name']} (exit {rc})")
                continue
            generated.append(out_html)

    print(f"\nGenerated {len(generated)} report(s) in {args.out_dir}/")


if __name__ == "__main__":
    main()
