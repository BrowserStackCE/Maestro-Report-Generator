# Maestro HTML Report Generator for BrowserStack

Turns the results of a Maestro run on **BrowserStack App Automate** into an easy-to-read
**HTML report**: test cases, every step with its status and time, the error message and the
failure screenshot. One report per testcase in the build.

## Quick start (3 steps)

You need Python 3.9 or newer. Nothing else to install.

**1. Add your BrowserStack credentials**

```bash
cp .env.example .env
```

Open `.env` and fill in your username and access key (BrowserStack > Account > Settings).

**2. Run it**

```bash
python3 fetch_and_report.py
```

**3. Open the reports**

Look in the `reports/` folder and open any `.html` file in your browser.

That's it. It uses your most recent Maestro build automatically.

### Want to try it first, without credentials?

```bash
python3 generate_report_2.py examples/Login.yaml \
  --commands-json examples/browserstack_commandlogs.sample.json \
  -o reports/example.html
```

Then open `reports/example.html`.

## Common options

Add these to step 2 if you need them.

| I want to... | Add this |
|---|---|
| Use a specific build instead of the latest | `--build <40-character build id>` |
| See test cases (TC01, TC02...) from my YAML files | `--flows-dir <folder with your YAML files>` |
| Only get reports for failed testcases | `--only-failed` |
| Save reports somewhere else | `--out-dir <folder>` |

Example: `python3 fetch_and_report.py --flows-dir tests/data --only-failed`

### Showing test cases (TC01, TC02...) in the report

By default each testcase shows as one "Full Flow" entry. To split it into test cases:

1. Put a comment above each group of steps in your flow YAML:

   ```yaml
   # ═══ TC01: Standard Login ═══
   - tapOn: "Email"
   ```

   Use the box character `═`, copied from `examples/Login.yaml`. Plain `=` signs are **not** recognised.

2. Run with `--flows-dir` pointing at the folder with your YAML files. Each BrowserStack testcase
   (for example `data/revzilla/Login`) is matched to a YAML file with the same ending
   (`.../revzilla/Login.yaml`). If a YAML points to another file with `runFlow: .../flows/Login.yaml`,
   that file is followed to find the markers. Your files are never changed.

## What each file does

| File | What it does |
|---|---|
| `fetch_and_report.py` | The one you run. Finds the build, downloads the logs and screenshots, and makes a report per testcase. |
| `generate_report_2.py` | Turns one downloaded command log into an HTML report. Called by the script above. |
| `examples/` | A sample log and sample YAML for trying it without credentials. |
| `.env.example` | Template for your credentials. Copy it to `.env`. |
| `requirements.txt` | Optional: Pillow, which makes screenshots smaller. |
| `.gitignore` | Keeps `.env` and `reports/` out of git. |

## If something goes wrong

| You see | Do this |
|---|---|
| `Set BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY` | Fill in `.env` (step 1). |
| `No Maestro build found` | The account has no Maestro builds in its last 100 builds. Pass `--build <id>`. |
| `is not valid JSON ... plain-text Maestro log` | You gave it the text log (`maestrologs`). It needs the JSON from `commandlogs`. |
| `note: no YAML ... matches '<name>'` | No YAML file (or more than one) matched that testcase. See the disclaimer below. |

## Disclaimer: what was tested, and what to know

**How the reports are built.** Everything comes from the build that ran on BrowserStack, not
from re-running tests. The script lists the build's sessions and every testcase in them, then
downloads each testcase's own command log (JSON) and failure screenshot. The command log decides
every step's status, duration and error. Your YAML files are used only to label and group steps
into test cases.

**What was verified**
- Run against a real Maestro build ("Build #39: browserstack-demoapp.ipa", 6 testcases across 2
  sessions): 6 reports were generated, each with the assertion-failure message and one
  failure screenshot.
- `--flows-dir` was tested on a replica of a `tests/data/<brand>/<Flow>.yaml` layout with
  shared `flows/`: each testcase was matched to its own YAML and the markers were found by
  following the `runFlow` pointer. The replica's step labels were made up, so only the matching
  (not the per-TC pass/fail split) was meaningful.
- With no credentials, the script stops with a one-line message, not an error trace.


**Things to know**
- **Use `commandlogs`, not `maestrologs`.** `maestrologs` is plain text and cannot be parsed. The
  session response also has a `commands_log` field whose link returns 404; the script uses
  `maestro_commands` instead.
- **Two sessions can share testcase names**, so report files end with a short testcase id to
  avoid overwriting each other.
- **No guessing on ambiguous matches.** If several files fit a testcase name (for example every
  brand has a `Login.yaml`) or none do, the script prints a note and that report uses a single
  "Full Flow" entry, instead of borrowing another brand's test cases.
- **`--yaml <file>`** applies one YAML to every testcase, so only use it for a build with a single flow.
- **Never commit `.env`.** It is git-ignored. If an access key was ever pasted into a chat,
  ticket or log, rotate it.
- **Do not publish `reports/`.** It holds device screenshots and log data from your builds. It is
  git-ignored, but check before zipping or sharing the folder.
