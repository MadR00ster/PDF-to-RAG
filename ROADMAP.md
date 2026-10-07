# Roadmap: a shared MCP server that answers, not only retrieves

Status: **planned, nothing built.** Written 2026-10-02. This file describes
work that does not exist yet; `SKILL.md` describes what does.

## Goal

One MCP server, reachable company-wide, serving a converted corpus to every
user's editor. A language model running beside the server reads the retrieved
sections and returns a short cited answer, so each client spends fewer of its
own credits.

## Where the repo is today

`scripts/mcp_server.py` is a content-feeding server. It finds sections in a
SQLite FTS5 index and returns their text; the client's model does all the
reading and answering, on that user's credits. It speaks stdio only, so each
user runs a private copy on their own machine.

Retrieval is measured (`references/retrieval-measurement.md`): hit@1 71%,
hit@5 92%, MRR 0.802 on 76 questions.

## What a client pays per question now

Measured 2026-10-02 by running the server's own `search()` and
`format_section()` on the 76 eval questions against the 13,767-chunk index.
Tokens are estimated at 3.5 characters each.

| what the client receives | median | 90th percentile |
|---|---|---|
| one `search_docs` result page (10 hits) | ~1,300 tokens | ~1,400 |
| search + `get_section` on the top 3 | ~5,400 tokens | ~8,000 |
| search + `get_section` on the top 5 | ~8,300 tokens | ~12,900 |
| one `lookup_entity` (300 sampled) | ~900 tokens | ~3,400 |
| tool schemas, sent with every request | ~1,250 tokens | |

A server-written answer with citations should be 300-500 tokens. Tool results
stay in the conversation and are re-sent on later turns, so the saving
compounds.

## POC assumptions

| | |
|---|---|
| users | about 5, all possibly asking at once |
| clients | GitHub Copilot in VS Code, and Cursor |
| client billing | per token in both (Copilot since 2026-06-01, Cursor since 2025-06), so fewer tokens returned means fewer credits spent |
| deployment host | a Linux server with Intel Gaudi accelerators; exact spec undecided |
| model runtime | vLLM with the Intel Gaudi plugin, which serves an OpenAI-compatible endpoint. Ollama and llama.cpp do not run on Gaudi |
| development workstation | not a deployment target and gets no model runtime. Code is developed and tested here against a stub endpoint; real-model runs happen on the Gaudi host |

## Target shape

```
VS Code / Cursor  --HTTPS + bearer token-->  reverse proxy (TLS)
                                                  |
                                           mcp_server.py --http
                                            |            |
                              mcp-index.sqlite3     vLLM on Gaudi
                              figures/, PDFs        (localhost only,
                                                     OpenAI-compatible)
```

## Design decisions

- **The existing tools stay.** `ask_docs` is added beside them, so a client
  that doubts an answer can still read the section itself.
- **The server talks to any OpenAI-compatible URL** using the standard
  library. Moving between runtimes or hosts is a change of URL and model name.
- **The model does not need tool calling.** The server retrieves, then makes
  one generation call. That widens the choice of models the Gaudi plugin
  supports.
- **The default stays as it is.** Without `--http` the server is stdio; without
  a model URL `ask_docs` is not listed. No packages, no network, no key.
- **If the model is down or slow, `ask_docs` returns the passages instead**, so
  the server degrades to content feeding rather than failing.
- **No embedding index.** Measured and rejected; see
  `references/retrieval-measurement.md`.

## Deployment options

Checked against Intel's Gaudi documentation on 2026-10-02 (Gaudi software
1.24.1). Versions move; re-check the support matrix before ordering or
installing.

### What has to be hosted

| service | needs |
|---|---|
| model server (vLLM) | a Gaudi card, Linux, Intel's Gaudi software stack |
| MCP server | CPU only. Python 3 with SQLite FTS5, and the corpus on disk |
| reverse proxy | TLS and sign-in, on the corporate network |

Only the model server needs the accelerator. The MCP server and proxy run on
any small Linux machine or VM. The corpus is small: the 24-manual corpus
measured here is about 1 GB (PDFs 0.5 GB, figures 0.34 GB, index 69 MB).

### Physical or virtual

| option | supported | notes |
|---|---|---|
| bare-metal Linux with Docker | yes | the path Intel's guides assume; fewest layers to debug |
| KVM virtual machine | yes, by PCI passthrough | Gaudi 2 and Gaudi 3; hosts on Ubuntu 22.04.5, 24.04.2 or RHEL 9.4 |
| VMware ESXi virtual machine | yes, by passthrough | Intel publishes a driver for the Gaudi 3 PCIe card on ESXi 8.0U3 and 9.1 |
| Kubernetes or OpenShift | yes | versions listed in the support matrix; more machinery than five users need |
| Windows or WSL | not listed | the support matrix names Linux only |

Passthrough hands whole cards to one VM. Intel's virtualization guide says PCI
passthrough is the only mechanism: no SR-IOV, no virtual GPU. A card cannot be
split between VMs, and a card given to a VM is gone from the host.

### Operating system

| source | lists |
|---|---|
| Gaudi software 1.24.1 support matrix | Ubuntu 22.04.5 (kernel 5.15+), Ubuntu 24.04.2 (kernel 6.8.0), RHEL 9.6 and 9.8 |
| vLLM Gaudi plugin quick start | Ubuntu 22.04 or 24.04, RHEL 9.4 or 9.6 |

Ubuntu 22.04, Ubuntu 24.04 and RHEL 9.6 are on both lists. The Gaudi driver is
a kernel module tied to the kernel version, so hold automatic kernel upgrades
and move the kernel and the Gaudi software together.

### Hardware

| | memory per card | power | form |
|---|---|---|---|
| Gaudi 3 PCIe card (HL-338) | 128 GB | 600 W | full-height, full-length, dual-slot PCIe Gen5 x16; fits a general GPU server with the slot, power and airflow for it |
| Gaudi 3 OAM module | 128 GB | 900 W | sold in eight-card systems, e.g. Supermicro SYS-822GA-NGR3 (8U) or Dell PowerEdge XE9680 (6U, about 11.5 kW) |
| Gaudi 2 | 96 GB (from memory; confirm in the product brief) | | |

Sizing, by arithmetic rather than measurement: weights take about 2 GB per
billion parameters at 16-bit and about 1 GB at FP8, plus working memory for
each request in flight.

- A 14-billion-parameter model is about 28 GB: one card, with room to spare.
- A 70-billion-parameter model is about 140 GB at 16-bit (two cards) or about
  70 GB at FP8 (one 128 GB card).

One card is expected to be enough for five users. An eight-card system is far
more than the POC needs. The host also needs disk for model weights, tens to
hundreds of GB depending on the models tried.

### Software stack

- **Host:** Intel Gaudi software 1.24.1 or later (driver, firmware, container
  runtime) and Docker.
- **Model server:** Intel's vLLM plugin image from `vault.habana.ai`, started
  with Docker Compose. It serves the OpenAI-compatible API `ask_docs` calls.
- **MCP server:** a container or a system service running `mcp_server.py
  --http`. Standard library, plus PyMuPDF for `get_page_image`.
- **Reverse proxy:** whatever the company already runs for internal TLS.

The plugin validates a fixed list of models, including Llama (8B, 70B, 405B),
Mistral 7B, Mixtral, Qwen 2.5 (14B and others) and Granite, each with the card
count it was tested on. Choose from that list.

### Network

- **Inbound:** only the proxy's port is open to the corporate network. vLLM
  binds to localhost, or to a private link if the services sit on two hosts.
- **Outbound, at install time:** container images come from `vault.habana.ai`
  and model weights from Hugging Face. If the server has no internet access,
  plan an internal mirror or an offline transfer before anything else.
- **Name and certificate:** clients need one stable URL, so ask for a DNS name
  and a certificate from the corporate authority early.

### Running it

- **Start-up is slow.** vLLM on Gaudi warms up for a set of input sizes before
  it serves, and large models take long; Intel documents settings that cut this
  by up to 90%. Run it as an always-on service, not on demand.
- **Upgrades come in pairs.** Each plugin version is built for a Gaudi software
  version; upgrade them together and re-run the answer eval afterwards.

### Recommended for the POC

One bare-metal host, Ubuntu 24.04, one Gaudi 3 card, Docker Compose with three
containers: vLLM, the MCP server, the proxy. Everything on one machine, nothing
shared, one place to look when it breaks.

- If company policy allows only VMs, use KVM or ESXi passthrough with one card
  dedicated to the VM. The rest of the layout is unchanged.
- If the company standard is RHEL, use 9.6.
- For a wider rollout, move the MCP server and proxy to an ordinary VM and
  leave the Gaudi host as a model endpoint other projects can share.

## To-do

### 0. Inputs needed before deployment

- [ ] Physical host or VM, decided with whoever runs the data centre. See
      "Deployment options".
- [ ] Gaudi generation, form (PCIe card or OAM system) and card count.
- [ ] Operating system: Ubuntu 24.04, or RHEL 9.6 if that is the standard.
- [ ] Does the host have internet access for images and model weights? If not,
      who provides the mirror or transfer?
- [ ] DNS name and TLS certificate for the server.
- [ ] Rack space, power and cooling for the chosen hardware.
- [ ] Confirm the vendor manuals' licence allows company-wide serving.
- [ ] Decide which corpus the POC serves and where it lives on the host.
- [ ] Decide how users authenticate: one shared token, a token per user, or the
      corporate proxy's sign-in.

### 1. HTTP transport

- [ ] `--http <host:port>` mode in `mcp_server.py`: MCP Streamable HTTP on one
      endpoint, JSON-RPC in, JSON out. stdio remains the default.
- [ ] Bearer-token check on every request; refuse to bind a non-loopback
      address without a token.
- [ ] One read-only SQLite connection per thread. `Corpus` holds a single
      shared connection today.
- [ ] Per-call log line (JSONL): time, user or token id, tool, query, characters
      returned, latency. This is also the source of real user questions for the
      eval set.
- [ ] Health endpoint reporting index path, document count and coverage.
- [ ] `mcp_smoke_test.py --url` to drive the same checks over HTTP.
- [ ] Emit client configs: VS Code (`servers`, `"type": "http"`) and Cursor
      (`mcpServers`, `url`), each with the auth header.
- [x] Fix `build_search_db.py`: `documents.slug` is the primary key, so two
      collections sharing a slug crash the build. A multi-vendor corpus hits it.
      Done: documents are keyed by collection and slug, and every server
      lookup names both.
- [ ] Verify both clients accept the server's responses (handshake, tool list,
      image content from `get_figure`).

### 2. Passage-level returns (the no-model baseline)

- [ ] `get_section` takes an optional `query` and returns only the passages
      that match it, with enough surrounding text to be read alone.
- [ ] Token accounting in `eval_search.py`: characters a client receives to
      reach the answer, per question.
- [ ] Record the baseline before and after. `ask_docs` has to beat this, not
      the current whole-section numbers.

### 3. `ask_docs`

- [ ] `--llm-url`, `--llm-model` and matching environment variables; the tool is
      listed only when they are set.
- [ ] Retrieve with the existing `search()`, pack top sections into a character
      budget sized to the model's context.
- [ ] Prompt: answer only from the supplied sections, quote identifiers
      exactly, cite section ids, say so when the sections do not answer.
- [ ] Return the answer, its citations (document, breadcrumb, page) and the
      section ids, so the client can verify with `get_section`.
- [ ] Timeout, a cap on concurrent generations, and the passage fallback.
- [ ] Tests against a stub OpenAI-compatible endpoint; no runtime needed to run
      the suite.

### 4. Answer evaluation

- [ ] `eval_answers.py` over the same `questions.jsonl`: does the answer cite a
      section listed in `answers`; does it abstain when retrieval missed;
      tokens returned.
- [ ] Faithfulness check on every answer in the set: no claim absent from the
      cited sections. By hand at this size, or with a judge model.
- [ ] Score figure questions separately. A text-only model sees OCR text, not
      the image.
- [ ] Proposed bar for keeping `ask_docs`: cites a correct section on at least
      as many questions as search puts one in its top three; no unsupported
      claims; median tokens returned under a fifth of the step-2 baseline.

### 5. Deployment on the Gaudi host

- [ ] Install the OS, hold kernel upgrades, install Intel Gaudi software and
      Docker; confirm the card is visible with Intel's `hl-smi` tool.
- [ ] vLLM with the Gaudi plugin, endpoint bound to localhost.
- [ ] Choose the model from the plugin's supported list by running step 4 on
      two or three candidates.
- [ ] Tune warm-up so a restart returns to service in an acceptable time.
- [ ] Run `mcp_server.py --http` as a service behind the reverse proxy.
- [ ] Docker Compose file and a short runbook: start, stop, upgrade, where the
      logs are.
- [ ] Copy corpus, index, figures and source PDFs; install PyMuPDF for
      `get_page_image`.
- [ ] Written procedure for rebuilding the index after a corpus change. The
      index is a snapshot.

### 6. POC run and decision

- [ ] Five users for an agreed period, logs on.
- [ ] Report: questions asked, tokens returned per question against the
      baseline, latency, and how often an `ask_docs` call is followed by
      `get_section` on the same sections (a sign the answer was not trusted).
- [ ] Add the logged questions to the eval set as kind `real`.
- [ ] Decide on wider rollout from those numbers.

### Later, only if the POC justifies it

- [ ] Rerank the top ten with the model. hit@1 is 71% against hit@5 at 92%;
      measure with `eval_search.py`.
- [ ] Sign-in through the corporate identity provider, per-user rate limits.
- [ ] More than one corpus behind one server.

## Risks

- **Answers a client does not trust cost more, not less.** It reads the answer
  and then the sections. Step 6 measures this.
- **Retrieval misses 8% of questions in its top five.** A model handed the
  wrong sections tends to answer anyway; the prompt and the eval both target
  abstention.
- **The saving per question is small in absolute terms.** At an assumed $3 per
  million input tokens, 5,000 tokens is about 1.5 cents. The case rests on
  volume, which the POC logs establish.
- **Cost moves rather than disappears.** Client credits become hardware and
  upkeep the company owns.
- **The Gaudi plugin lags upstream vLLM** and supports a fixed list of models.

## Sources for the assumptions

- Copilot billing: <https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/>
- Cursor billing: <https://www.vantage.sh/blog/cursor-pricing-explained>
- Remote MCP servers in VS Code: <https://code.visualstudio.com/docs/agent-customization/mcp-servers>
- vLLM on Gaudi, features: <https://docs.vllm.ai/projects/gaudi/en/latest/features/supported_features.html>
- vLLM on Gaudi, quick start and validated models: <https://docs.vllm.ai/projects/gaudi/en/latest/getting_started/quickstart/quickstart.html>
- Gaudi support matrix: <https://docs.habana.ai/en/latest/Support_Matrix/Support_Matrix.html>
- Gaudi virtualization (KVM): <https://docs.habana.ai/en/latest/Virtualization/Configuring_VMs_on_Gaudi.html>
- Gaudi passthrough driver for VMware ESXi: <https://www.intel.com/content/www/us/en/download/860900/passthrough-driver-for-intel-gaudi-ai-accelerators-for-vmware-esxi.html>
- Gaudi 3 PCIe card and server options: <https://www.dell.com/en-us/blog/experience-choice-in-ai-with-poweredge-and-gaudi3/>, <https://www.supermicro.com/en/products/system/datasheet/sys-822ga-ngr3>
