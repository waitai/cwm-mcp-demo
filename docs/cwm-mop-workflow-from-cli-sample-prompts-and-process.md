# How the `cwm-mop-workflow-from-cli-sample` skill came about

This note records the **conversation arc** and **representative prompts** that led to the Cursor skill at [`.cursor/skills/cwm-mop-workflow-from-cli-sample/SKILL.md`](../.cursor/skills/cwm-mop-workflow-from-cli-sample/SKILL.md). Use it to **replay the workflow** with the agent or to onboard others.

The skill itself is the **durable procedure**; this document is the **human story + prompt templates**.

---

## 1. Precursors (same thread / repo session)

These steps shaped what the skill encodes (they are not the skill file, but they motivated it):

| Topic | What you asked (paraphrased) | Why it matters for the skill |
|--------|------------------------------|------------------------------|
| Command capture | Run **command-capture** with mopActivity args and a CLI command; fix payload shape (`commandCapture` at **top level** of `data`, not only under `app-data.data`). | Clarifies **job `data`** layout vs workflow **input schema**. |
| Vendor gate | Why **“Selected vendor is not supported”** for command-capture; inspect workflow. | Documents **`app-data.device.vendor`** for **command-capture-cwm-sol** (`Cisco Systems` / `Juniper Networks`). |
| Hostname workflow | Build **cisco-hostname-verify-cwm-sol** from **`cisco-disk-space-cwm-sol.sw.json`**, sample `show running-config hostname` + XR output, full Cisco product-series gate; **deploy** and **test** via bridge. | Established the **reference skeleton + TextFSM + post_workflow + post_job** pattern the skill generalizes. |
| Show version | Invoke the skill explicitly with **`show version`**, sample XR output, parse **non-empty Version**; deploy **cisco-show-version-parse-cwm-sol** and test. | Second full pass proving the skill’s workflow. |

---

## 2. The prompt that created the skill

You asked for a **Cursor skill** that would:

1. Create a mopActivity workflow from a **sample CLI command and output** (TextFSM + util-executor style).
2. **Deploy** to CWM (`post_workflow` via MCP bridge).
3. **Test** with **known-good** device/resource/product series when available, or **prompt for input** if not.

**Representative wording (you can reuse):**

```text
Can you create a Cursor skill to do what you have just done with creating a workflow
from a sample cli command and output, deploy the workflow into CWM and test it with
known good data or prompt the user for the input data to be used if none available?
```

**Implementation choice:** Project skill under **`.cursor/skills/cwm-mop-workflow-from-cli-sample/`** (`SKILL.md` + `reference.md`) so it ships with this repository.

---

## 3. Follow-up prompts (operating the skill)

| Goal | Example prompt |
|------|----------------|
| Enable / find skills | “How do I enable project skills in Cursor?” |
| Test the skill | “How do I test this cursor skill?” — then use **`/`** + skill name or natural language that matches the **description**. |
| Run one full cycle | Attach skill + supply command, sample output, reference workflow, deploy/test instructions (see §4). |

---

## 4. Template: full-cycle prompt (copy-paste)

Use with **Agent**; prefer **`/cwm-mop-workflow-from-cli-sample`** so the skill is definitely in context.

```text
/cwm-mop-workflow-from-cli-sample

Using the cwm-mop-workflow-from-cli-sample skill:
Command: <exact CLI>
Sample output:
<paste multi-line device output>

Reference workflow: cisco-disk-space-cwm-sol.sw.json  (download from CWM with MCP tools via the bridge first; see root README “Cursor skill: reference workflow”)
Create a new mopActivity workflow that parses this output and <validation goal>,
save as <name>.sw.json,
then deploy with scripts/post_workflow_from_file.py and test with post_job.
Use NCS540X-7, Cisco Network Convergence System 540 Series Routers, and cwm.sol.system.nso
if you have no other test data; otherwise ask me for device, productSeries, and resource.
```

Adjust **validation goal**, **workflow file name**, and **test defaults** as needed.

---

## 5. Artifacts tied to this story

| Artifact | Role |
|----------|------|
| `.cursor/skills/cwm-mop-workflow-from-cli-sample/SKILL.md` | Agent procedure |
| `.cursor/skills/cwm-mop-workflow-from-cli-sample/reference.md` | Deploy/job JSON shapes |
| `cisco-hostname-verify-cwm-sol.sw.json` | First custom workflow from the pattern |
| `cisco-show-version-parse-cwm-sol.sw.json` | Second workflow (skill demo) |
| `scripts/post_workflow_from_file.py` | Deploy wrapper (`post_workflow`) |
| `scripts/run_job_from_input.py` | Test job wrapper (`post_job`) |
| `tmp-*-deploy.json` / `tmp-*-job.json` | Ephemeral payloads (regenerate anytime) |

---

## 6. Link from the skill

For progressive disclosure, the skill’s [reference.md](../.cursor/skills/cwm-mop-workflow-from-cli-sample/reference.md) can point here under an **“Origin / prompts”** bullet so `SKILL.md` stays short.
