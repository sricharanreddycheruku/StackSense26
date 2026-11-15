# StackSense

StackSense is a rule-based tech stack recommendation engine. Give it a project scenario (type, platforms, scale, team, budget, timeline, infrastructure, requirements, compliance and priorities) and it returns a frontend, backend, database and hosting choice plus the supporting services the project needs (auth, storage, cache, queue, search, realtime, payments, ML, CDN, CI/CD, observability, and a CMS or BI dashboard tool when the project calls for one). Every component comes with a justification that quotes the input values behind it, the input fields that drove it, at least one rejected alternative with the reason, and known trade-offs. The engine also estimates a monthly cost band, validates the input field by field, flags contradictions with a severity, and never silently breaks a hard constraint (data residency, compliance, deployment model, cloud preference) or the budget. All decisions come from a curated, cited knowledge base and deterministic scoring code. No language model is involved, so the same input always gives the same output.

Source repository: https://github.com/sricharanreddycheruku/StackSense26

## System architecture

```
 scenario JSON (file, UI form or API)
        │
        ▼
 ┌──────────────┐   field-level errors ──► status "invalid_input", stack null
 │ validate.py  │   defaults ──► assumptions[]
 └──────┬───────┘
        ▼
 ┌──────────────┐   ┌──────────────┐
 │ describe.py  │──►│  derive.py   │  scale tier, regions, effort estimate,
 └──────────────┘   └──────┬───────┘  sensitivity, connectivity, weights
   keyword signals          ▼
 ┌──────────────────┐  ┌──────────────────┐
 │ constraints.py   │◄─┤ kb_loader.py     │◄── kb/*.json (cited knowledge base)
 │ hard filters,    │  └──────────────────┘
 │ conflicts,       │
 │ relaxations      │
 └──────┬───────────┘
        ▼
 ┌──────────────────┐   ┌──────────────┐   ┌──────────────┐
 │ architecture.py  │──►│ recommend.py │──►│   cost.py    │  monthly band + assumptions
 └──────────────────┘   └──────┬───────┘   └──────┬───────┘  (budget loop re-selects
     style rules         score + select           │           cheaper options if needed)
                               ▼                  ▼
                        ┌──────────────┐   ┌──────────────┐
                        │ conflicts.py │──►│  explain.py  │  justification, driven_by,
                        └──────────────┘   └──────┬───────┘  alternatives, trade-offs
                          warnings[]              ▼
                                           ┌──────────────┐
                                           │  engine.py   │──► output JSON (spec §4)
                                           └──────────────┘        │
                                                   ▲               ├──► cli.py  (files)
                                                   └───────────────┴──► app.py  (web UI, API)
```

| Module | Role |
|---|---|
| `validate.py` | Checks types, ranges and enums for every field. Collects all errors at once, applies documented defaults and records each one as an assumption. Never raises. |
| `describe.py` | Keyword rules over `project.description` that suggest structured fields (for example "2G/3G" suggests intermittent connectivity). Suggestions are shown in the UI for the user to confirm. |
| `derive.py` | Turns the validated input into derived features: scale tier, user regions, estimated build effort, data sensitivity, consistency needs and priority weights. |
| `kb_loader.py` | Loads and checks the knowledge base once (read-only, cached). |
| `constraints.py` | Removes every option that breaks a hard constraint, detects hard-vs-hard conflicts (status `infeasible`) and records relaxations. |
| `architecture.py` | Picks the architecture style from a rule table (static, monolith, modular monolith, serverless, service-oriented, hybrid). |
| `recommend.py` | Scores the remaining options per layer with weighted criteria and picks the core and supporting components. Ties break on a fixed key, so results are stable. |
| `cost.py` | Builds an order-of-magnitude monthly cost band from per-component price anchors and states its assumptions. |
| `conflicts.py` | Soft conflicts and notes: budget, timeline, skills, availability, deprecated or end-of-life systems, compliance notes. |
| `explain.py` | Writes the justification, `driven_by`, alternatives with `why_not`, and trade-offs for each component. |
| `engine.py` | Orchestrates the steps, sets the status and returns the output object. |
| `cli.py`, `app.py` | Command line and web front ends over `engine.run()`. |

Data flow: raw JSON → validated spec + assumptions → derived features → filtered candidates per layer → scored and selected stack → cost band → warnings → explained output JSON. Every step is a pure function of its input and the knowledge base, so the output is deterministic.

## Technology used

| Component | Version tested |
|---|---|
| Python | 3.12.3 (engine also tested on 3.10) |
| Engine, CLI, tests | Python standard library only |
| FastAPI (web UI and API only) | 0.162.2 |
| Uvicorn | 0.54.0 |
| Starlette, Pydantic (FastAPI dependencies) | 1.10.0, 2.14.3 |
| Web UI | Plain HTML, CSS and JavaScript (no build step, no CDN, works offline) |

## Prerequisites

- Python 3.10 or newer.
- Windows: use `py -3` (or `python`) in PowerShell. macOS and Linux: use `python3`.
- About 50 MB of disk space for the virtual environment. No internet is needed at run time.
- No API keys or paid services.

## Local setup and run

The command line engine needs no install. The web UI needs FastAPI and Uvicorn.

**Windows (PowerShell)**

```powershell
cd source
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --port 8000
```

**macOS / Linux**

```bash
cd source
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app:app --port 8000
```

Open http://localhost:8000. Pick a sample from the dropdown (P1-P6 or an edge case), or fill the form, or use **Import JSON**, then press **Run recommendation**. **Suggest fields from description** proposes field values from the free-text description for you to tick and apply. **Download output JSON** saves the result in the spec §4 format (the same file the CLI writes), and **Download input JSON** saves the scenario you built in the form.

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope Process RemoteSigned` first, or skip activation and call `.venv\Scripts\python -m uvicorn app:app --port 8000`. If port 8000 is busy, use `--port 8001`.

## Run a scenario JSON file

From the `source` folder (no virtual environment needed):

```bash
# one file -> out/P2_output.json
python -m stacksense run scenarios/public/P2.json

# one file to a chosen path
python -m stacksense run my_scenario.json -o result.json

# every *.json in a folder
python -m stacksense run scenarios/public --out-dir out

# print to the terminal instead of writing a file
python -m stacksense run scenarios/public/P4.json --stdout

# regenerate the submitted outputs/P1_output.json ... P6_output.json
python -m stacksense run scenarios/public --out-dir ../outputs
```

On Windows replace `python` with `py -3` if `python` is not on the path.

- Output files are named `<scenario_id>_output.json` (or the file name when there is no `scenario_id`) and are written to `out/` by default.
- A path can be a single scenario file, a folder, or a combined file holding a JSON array of scenarios (or `{"scenarios": [...]}`, like `stacksense_public_scenarios.json`).
- Besides the fields of the problem statement, each output has a `capacity` block (peak users, concurrency, requests per second, API instances and 12-month data volume, with the basis of each figure) and `description_signals`.
- A broken or invalid file still produces an output with `"status": "invalid_input"` and every problem listed in `validation_errors`.
- `python -m stacksense validate FILE` checks a file without recommending. `python -m stacksense describe "free text"` shows the fields suggested from a description.

## Switching off LLM features

StackSense makes no LLM calls at all. Description parsing uses keyword rules and stack selection uses the scoring code. The switch required by the rules still exists and defaults to off:

- environment variable `STACKSENSE_LLM=off` (see `.env.example`), or
- CLI flag `--llm off`.

Both settings give identical stacks, and the output records `"llm": {"enabled": false}`.

## Tests and checks

```bash
# unit, scenario, edge-case, determinism and CLI tests (standard library)
python -m unittest discover -s tests -p "test_[!a]*.py"

# API tests (inside the virtual environment)
python -m pytest tests/test_api.py -q

# randomized sweep: 10,000 generated and mutated scenarios, checks invariants
# (--summary also writes the counts per invariant as JSON)
python tools/fuzz_sweep.py --n 10000 --seed 26 --summary fuzz_summary.json

# sensitivity study: change one input field at a time on P1-P6 and count what moves
python tools/sensitivity.py
```

## Key results

- 235 unit tests pass (standard library), plus 9 API tests with pytest.
- Fuzz sweep, 10,000 inputs (seed 26): all 13 invariants hold with 0 failures; about 66 ms per input. Counts per invariant: `tools/fuzz_summary.json`.
- Sensitivity study: 729 single-field variants of P1-P6; 42 of 43 fields change the output, and only the display field `project.name` changes nothing. Table: `tools/sensitivity_results.md`.
- Knowledge base: 241 entries in 22 layers, 209 provider offerings, 66 providers, 584 cited URLs.

## Project structure

```
source/
├── README.md, requirements.txt, .env.example
├── app.py                      FastAPI app: web UI + JSON API
├── web/                        index.html, app.js, style.css
├── stacksense/
│   ├── __main__.py, cli.py     command line
│   ├── engine.py               run(scenario) -> output
│   ├── validate.py, schema.py  input schema, validation, defaults
│   ├── describe.py, derive.py  description signals, derived features
│   ├── constraints.py, architecture.py, recommend.py, cost.py
│   ├── conflicts.py, explain.py, kb_loader.py, model.py
│   └── kb/                     knowledge base (JSON, every entry cites its sources)
├── scenarios/public/           P1-P6 from the problem statement
├── scenarios/edge/             edge cases (invalid input, contradictions, regulation, legacy systems ...)
├── tools/                      fuzz_sweep.py, sensitivity.py, kb_report.py and their saved results
└── tests/                      unittest suites
```

API endpoints: `GET /api/schema`, `GET /api/samples`, `GET /api/samples/{group}/{name}`, `POST /api/recommend` (body: scenario JSON), `POST /api/describe` (body: `{"text": "..."}`).

## Credits

- [FastAPI](https://fastapi.tiangolo.com/) (MIT), [Uvicorn](https://www.uvicorn.org/) (BSD-3-Clause), [Starlette](https://www.starlette.io/) (BSD-3-Clause), [Pydantic](https://docs.pydantic.dev/) (MIT), [pytest](https://pytest.org/) and [HTTPX](https://www.python-httpx.org/) for the API tests.
- Knowledge base sources: official documentation, pricing pages, compliance and region pages of each vendor, end-of-life notices, and regulatory texts (RBI, DPDP Act, GDPR, HIPAA, PCI DSS). Every knowledge base entry stores its source URLs in `stacksense/kb/*.json`, and the full list is in the report's references section (`python tools/kb_report.py` prints it).
- No external APIs or datasets are called at run time.
