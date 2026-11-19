# Cohere Rerank on Microsoft Foundry, plus a local ONNX fallback

Research date: 2026-11-19. Sources are listed at the end with their page dates. "Verified" means an official Microsoft or Cohere page states it, or I checked it live (Azure Retail Prices API, Hugging Face API, curl HEAD). "Reported" means it comes only from community samples or Q&A threads.

## TL;DR

| | Value | Confidence |
|---|---|---|
| Default model | `Cohere-rerank-v5-pro` (cheaper: `Cohere-rerank-v5-fast`). **Preview** on Foundry, Microsoft retirement date for this preview version is 2027-02-12. | Verified (MS retirement schedule, 2026-11-18) |
| Fallback hosted model | `Cohere-rerank-v4.0-pro` / `Cohere-rerank-v4.0-fast`. GA, retirement 2027-09-14, replacement v5. | Verified |
| Rerank 3.5 | `Cohere-rerank-v3.5` retired on Foundry 2026-05-14 and is no longer listed. | Verified |
| Endpoint | `POST https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank` | Reported. No Microsoft Learn page documents this route. |
| Body | Cohere v2: `model` (the **deployment name**), `query`, `documents` (list of strings), optional `top_n`, `max_tokens_per_doc` | Verified (Cohere API ref); deployment name as `model` reported |
| Response | `{id, results: [{index, relevance_score}], meta: {api_version, billed_units}}`. Results come sorted by score, highest first. | Verified (Cohere API ref) |
| Auth, key | `Authorization: Bearer <key>` (C# sample, Cohere SDK). `api-key: <key>` also reported. | Reported |
| Auth, Entra ID | Bearer token. Scope `https://ai.azure.com/.default` (MS docs for Foundry Models) or `https://cognitiveservices.azure.com/.default` (the v5 rerank sample). Role: **Cognitive Services User** on the Foundry resource. | Scope and role verified for Foundry Models in general, not specifically for the Cohere route |
| Score range | 0..1. v5 is **calibrated**: 0.50 (Pro) / 0.45 (Fast) means "more likely useful than not". v4 is not calibrated. | Verified (Cohere) |
| Limits | v5: up to 10,000 docs per request, 32k context (query + doc), `max_tokens_per_doc` defaults to 4096. Query max 2048 tokens. | Verified (Cohere) |
| Price, v5 | $0.05 (Pro) / $0.02 (Fast) per 1M tokens, Global. Data Zone meters: $0.06 / $0.024 in EU regions. | Verified (Azure Retail Prices API) |
| Price, v4 | $2.50 (Pro) / $2.00 (Fast) per 1K search units, Global. Data Zone: $2.75 / $2.20. | Verified (Azure Retail Prices API) |
| Yearly cost for lexwatch | Under $1/year on v5 for daily scoring, about $5 for a full rescore. See the cost section. | Estimate |
| Local fallback | `onnx-community/bge-reranker-v2-m3-ONNX`: `onnx/model_int8.onnx` (571 MB, single file) or `onnx/model.onnx` + `onnx/model.onnx_data` (2.27 GB fp32), plus `tokenizer.json`. Inputs `input_ids`, `attention_mask`; output `logits` `[batch, 1]`; apply sigmoid. | Verified (HF API, HEAD, graph strings) |

## 1. Models on Foundry

Official lists (Microsoft "Foundry Models sold by Azure", updated 2026-10-29, and the retirement schedule, updated 2026-11-18):

| Foundry model / deployment name | Cohere API id | Lifecycle on Foundry | MS retirement date | Replacement |
|---|---|---|---|---|
| `Cohere-rerank-v5-pro` | `rerank-v5.0-pro` | Preview (announced 2026-10-21) | 2027-02-12 | none |
| `Cohere-rerank-v5-fast` | `rerank-v5.0-fast` | Preview | 2027-02-12 | none |
| `Cohere-rerank-v4.0-pro` | `rerank-v4.0-pro` | GA (preview from 2025-12-11) | 2027-09-14 | `Cohere-rerank-v5-pro` |
| `Cohere-rerank-v4.0-fast` | `rerank-v4.0-fast` | GA | 2027-09-14 | `Cohere-rerank-v5-fast` |
| `Cohere-rerank-v3.5` | `rerank-v3.5` | Retired 2026-05-14 (legacy 2026-01-14, deprecated 2026-02-14) | | v4.0 |

Notes:
- Names are case-sensitive. Use them exactly as written above. The v4 names contain `v4.0`, but the v5 names do not (`v5-pro`, not `v5.0-pro`). Cohere's own platform table lists the v4 Foundry names as `cohere-rerank-v4-pro`, which does not match Microsoft's `Cohere-rerank-v4.0-pro`. Trust Microsoft's name. The value sent as `model` is the name **you gave the deployment**, which by default is the model name.
- Both v4 and v5 are "Direct from Azure" (sold, hosted and billed by Azure), deployed into a Foundry resource. They are not the old serverless/MaaS endpoints (`https://<name>.<region>.models.ai.azure.com` or `*.inference.ai.azure.com`). Cohere's Azure page still shows those older URLs with `v1/rerank` in its examples. That page is outdated for v4 and v5.
- A preview retirement date only 4 months after launch is normal for Foundry previews. The date usually moves out at GA. Watch the retirement schedule. The build prompt already makes the model name configuration.
- The Foundry catalog card for v5 shows "Context length 4096". This contradicts Cohere's documented 32k and is probably a catalog placeholder. Not verified.

### Deployment types and regions

From the region availability page (updated 2026-10-19):
- **v4.0 Pro/Fast, Global Standard:** all listed Americas, Europe, APAC and MEA regions. Europe: francecentral, germanywestcentral, italynorth, norwayeast, polandcentral, spaincentral, swedencentral, switzerlandnorth, switzerlandwest, uksouth, ukwest, westeurope.
- **v4.0 Pro/Fast, Data Zone Standard (EU):** francecentral, germanywestcentral, italynorth, polandcentral, spaincentral, swedencentral, westeurope.
- **v5 Pro/Fast:** not yet on that page. A Microsoft Q&A thread from 2026-10-23 says v5 is **Global Standard only**, with no EU Data Zone. However, the Azure Retail Prices API has listed `Rerank V5 Pro DZ` / `Rerank V5 Fast DZ` meters since **2026-11-01**, including EU regions (westeurope, swedencentral, francecentral, germanywestcentral). EU Data Zone for v5 is probably arriving now. Check the portal's deployment-type dropdown when deploying.
- Global Standard can process a request in any region. Data Zone EU keeps processing inside the EU data boundary, which may include EFTA countries. lexwatch sends only public bill text and user-written rule texts. The build prompt allows a hosted endpoint, but the README should say which deployment type was chosen.
- No regional Standard deployment exists for Cohere rerank.

## 2. Endpoint and request/response format

### URL

```
POST https://<resource>.services.ai.azure.com/providers/cohere/v2/rerank
Content-Type: application/json
```

- `<resource>` is the Foundry (AI Services) resource name. Configure the base as the resource root, with no `/models` and no `/openai`. Appending `/models` gives `404 Resource not found` (C# v5 sample).
- The Foundry portal's "endpoint" field has been reported to show a different URL for Cohere deployments. Build the URL yourself from the resource name.
- No Microsoft Learn page documents this route. The evidence is consistent across a Cohere-on-Foundry C# v5 sample (2026-10-30), Microsoft Q&A threads (2026-02 and 2026-03), TrueFoundry gateway docs, and an llmgateway issue (2026-11-06). Microsoft's own models page uses the same `/providers/<publisher>/...` pattern for Black Forest Labs.
- Cohere's Python SDK v1 client posts to `/v1/rerank`, which returns 404 on this route. lexwatch uses httpx directly, so this does not matter.
- Recommendation: make the full URL configurable (`FOUNDRY_RERANK_URL`), or make the base URL plus path configurable, with the path defaulting to `/providers/cohere/v2/rerank`.

### Request body (Cohere v2 rerank)

```json
{
  "model": "Cohere-rerank-v5-pro",
  "query": "Lutte contre le blanchiment de capitaux et le financement du terrorisme",
  "documents": [
    "Projet de loi portant modification de la loi modifiée du 12 novembre 2004 relative à la lutte contre le blanchiment ...",
    "Projet de loi relatif aux aides financières pour études supérieures ..."
  ],
  "top_n": 50,
  "max_tokens_per_doc": 4096
}
```

| Field | Type | Notes |
|---|---|---|
| `model` | string, required | The deployment name. Required on Foundry. Omitting it or misspelling it gives 404 `DeploymentNotFound` or 500 (both reported). |
| `query` | string, required | Max 2048 tokens. Cohere says Rerank 5 "doesn't support instructions that change how relevance is judged", so write rule texts as topic descriptions, not as instructions. |
| `documents` | string[], required | Plain strings. The v1 object form with `rank_fields` does not exist in v2. Cohere recommends YAML strings for structured data. |
| `top_n` | int, optional | Omit it to get every document back. lexwatch needs a score for every bill, so omit it. |
| `max_tokens_per_doc` | int, optional | Default 4096. Longer documents are truncated. |
| `relevant_spans` | object, optional, v5 only | Context Focus: `{"mode":"enabled","threshold":0.1,"max_spans":10}`. Cohere says Context Focus "is available through the Cohere API". It is not confirmed on Foundry, and lexwatch does not need it. |
| `priority` | int, optional | Cohere API only. Not needed. |

`return_documents` is not part of the v2 API. Map results back with `results[].index`.

### Response body (200)

The fixture is `tests/fixtures/foundry/rerank_response.json`. Its shape follows Cohere's documented v2 example for `rerank-v5.0-pro`.

```json
{
  "id": "3b8f6c1e-...",
  "results": [ {"index": 2, "relevance_score": 0.917}, {"index": 0, "relevance_score": 0.638} ],
  "meta": { "api_version": {"version": "2"}, "billed_units": {"input_tokens": 412} }
}
```

- `results` is ordered by `relevance_score`, highest first. `index` points into the request's `documents` list. Do not assume the results come back in input order.
- `meta` and every field inside it are optional in the schema. `billed_units` can contain `search_units` (the v4 and v3.5 billing unit, used in Cohere's older examples) or `input_tokens` (the v5 example). Parse both and store whichever is present. If neither is present, store null. Whether Foundry passes `meta` through is not verified. Record what comes back.
- Fields in `meta` that you can ignore: `tokens`, `cached_tokens`, `warnings`.

### Errors

Cohere's documented error body (all codes) is `{"id": "...", "message": "..."}`. The documented codes are 400, 401, 403, 404, 422, 429 ("Too many requests"), 498, 499, 500, 501, 503 and 504. The fixture is `tests/fixtures/foundry/rerank_error_429.json`. Only the shape is documented. The message text in the fixture is a placeholder.

On Foundry, the gateway can answer before Cohere does, with a different envelope. A real 500 reported on this route (2026-02-24) looked like this:

```json
{"statusCode": 500, "message": "Internal server error", "activityId": "66db4ff2-..."}
```

Other reported errors: `401` for a bad key, a missing role or the wrong tenant; `404 Resource not found` for a wrong path; `404 DeploymentNotFound` for a wrong `model`. Microsoft's quota page says a 429 carries a **`Retry-After`** header, and clients should back off exponentially and honor it. Recommendation: read `message`, then `error.message`, then the raw text. Retry 429, 500, 502, 503 and 504 with backoff, honoring `Retry-After` when present. Log `id` / `activityId` / `apim-request-id` for support tickets.

There are open, unresolved Q&A reports (2026-02 and 2026-03) of persistent 500s from `Cohere-rerank-v4.0-fast` on this route, including one where the user authenticated with Entra ID. One moderator claimed Entra tokens are "not fully supported" on the provider route "in some regions". No documentation backs that claim. The C# v5 sample states that Entra ID works. Test both auth modes during setup.

## 3. Authentication

**API key** (Foundry resource key, from Keys and Endpoint):
- `Authorization: Bearer <key>` is used by the C# v5 sample and the Cohere SDK. A Q&A user got 401 with a wrong key and passed auth with a correct one.
- `api-key: <key>` is the standard Azure AI Services header and is also reported to work on this route. Not verified.
- Default to `Authorization: Bearer <key>`, and allow `api-key` through configuration if needed.

**Entra ID (keyless)**, via azure-identity `DefaultAzureCredential` / `ManagedIdentityCredential`:
- Send `Authorization: Bearer <access token>`.
- Scope: Microsoft's current Foundry Models docs (2026-08-31 and 2026-10-19) say "Tokens must be issued with scope `https://ai.azure.com/.default`" (CLI: `az account get-access-token --resource https://ai.azure.com`). The only rerank-specific sample (C# v5) uses `https://cognitiveservices.azure.com/.default`. Foundry resources are Cognitive Services accounts and historically accept the cognitiveservices audience. Make the scope configurable, defaulting to `https://cognitiveservices.azure.com/.default`, and document `https://ai.azure.com/.default` as the alternative. Check which one works at setup time.
- Role: assign **Cognitive Services User** to the app identity (managed identity or service principal) at the **Foundry resource** scope. Owner and Contributor do **not** grant data-plane access. A custom role needs the data action `Microsoft.CognitiveServices/accounts/MaaS/*`. Assignments can take up to 5 minutes to propagate.
- Cache the token and refresh it before it expires. azure-identity's `get_token` returns `expires_on`.

## 4. Limits, pricing, cost estimate

### Limits

| | v5 Pro/Fast | v4.0 Pro/Fast |
|---|---|---|
| Context per (query + doc) | 32,768 tokens | 32,768 tokens. Longer documents are chunked into 32,764-token pieces. |
| Query | max 2048 tokens. The v4 best-practices page says the query can use up to half the context. | same |
| Docs per request | max 10,000 (error above that). Cohere recommends 1,000 or fewer. | `docs × max_chunks_per_doc` ≤ 10,000 |
| `max_tokens_per_doc` | default 4096 | default 4096 |
| Foundry rate limits | Cohere rerank has no specific row. "Rest of models" default: 400,000 TPM, 1,000 RPM, 300 concurrent requests per subscription, region and model. Subscription-level quota tiers are being rolled out (from 2026-05-07). | same |

lexwatch's volume is far below every limit. Batch about 100 bills per request.

### Pricing (Azure Retail Prices API, queried 2026-11-19, USD)

| Meter | Global | Data Zone (EU regions) | Unit | Effective |
|---|---|---|---|---|
| Rerank V5 Pro | 0.05 | 0.06 | per 1M tokens | 2026-11-01 |
| Rerank V5 Fast | 0.02 | 0.024 | per 1M tokens | 2026-11-01 |
| Rerank v4 Pro | 2.50 | 2.75 | per 1K search units | 2026-03-01 (DZ 2026-06-01) |
| Rerank v4 Fast | 2.00 | 2.20 | per 1K search units | 2026-03-01 (DZ 2026-06-01) |

- **Search unit** (v4): Cohere's pricing FAQ says "one query with up to 100 documents to be ranked". Each chunk of a long document counts as an extra document. 101 documents cost 2 SU.
- **Tokens** (v5): billed input tokens are roughly the sum, over documents, of (query tokens + document tokens). In Cohere's example, 5 short docs plus a 9-word query were billed 192 input tokens. The query is counted once per document.

### Cost estimate for lexwatch

Assumptions: 250 new bills/year, 40 profiles × 3 rules = 120 rules, about 400 tokens per bill (French title plus summary), about 30 tokens per rule. The full rescore covers 2,500 bills (about 10 years of history).

| Scenario | v5 Pro (DZ, $0.06/M) | v5 Fast (DZ) | v4 Pro (DZ, $2.75/1K SU) |
|---|---|---|---|
| Daily scoring, 1 request per rule per run with new bills (about 150 runs/year) | 250 × 120 × 430 ≈ 12.9M tokens → **$0.77/yr** | $0.31/yr | 150 × 120 = 18,000 SU → **$49.50/yr** |
| Worst case: 1 request per (bill, rule) | same tokens → $0.77 | $0.31 | 30,000 SU → $82.50 |
| Full rescore, all rules over 2,500 bills (25 SU per rule at 100 docs/request) | 2,500 × 120 × 430 ≈ 129M tokens → **$7.74** | $3.10 | 120 × 25 = 3,000 SU → **$8.25** |
| Rescore of one profile after a rule edit (3 rules × 2,500 bills) | 3.2M tokens → $0.19 | $0.08 | 75 SU → $0.21 |

Conclusion: cost is negligible on either model generation. On v4, batch bills (up to 100 per request) and send one request per rule, because billing is per request. On v5, batching saves nothing, but it keeps the number of requests and the latency down.

## 5. Score calibration and thresholds

- All Cohere rerank scores are in [0, 1]. Cohere's API reference warns that the scores are ordinal: "a score of 0.9 [does not mean] 2× more relevant than 0.45".
- **Rerank 5 is calibrated** (Cohere launch post and docs, 2026-10-21). `relevance_score` "reads as the probability that the document is useful". Expected calibration error is about 0.06, against 0.23 to 0.38 for Rerank 4 Pro and the Voyage rerankers. Suggested cutoffs: **0.50 for Pro, 0.45 for Fast**. Across benchmarks the best Pro threshold stayed between 0.47 and 0.53, and Pro and Fast are within 0.05 of each other. Averaging scores across rules and applying a single profile threshold is therefore reasonable on v5.
- **Rerank 4 is not calibrated.** Scores are query-dependent. Cohere's method for choosing a threshold: take 30 to 50 representative queries, pair each with a borderline-relevant document, and use the average score as the threshold. If lexwatch falls back to v4, use a separate default threshold calibrated on real data, and rescore everything when the model changes.
- Consequence for lexwatch: the default threshold belongs to the model. Store `model` with every score, and rescore plus recheck thresholds whenever `FOUNDRY_RERANK_MODEL` changes. Start with 0.5 on v5 Pro, then calibrate on real bills as the build prompt requires.
- The local bge scores (sigmoid of a logit) are on a different scale from Cohere's and are not calibrated. They need their own threshold.

## 6. Local fallback: BAAI/bge-reranker-v2-m3 as ONNX

Base model: `BAAI/bge-reranker-v2-m3` (Apache-2.0, XLM-RoBERTa-large cross-encoder, about 568M parameters, multilingual including French). The upstream repo ships only `model.safetensors` (2.27 GB) and no ONNX. Its revision is `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`.

**Recommended ONNX repo:** `onnx-community/bge-reranker-v2-m3-ONNX` (the Hugging Face-maintained export, 87k downloads). Revision: `6f5ff65298512715a1e669753bc754d2bc8f367b` (last modified 2025-09-01). Files, with sizes from the HF API:

| File | Size | Use |
|---|---|---|
| `onnx/model.onnx` | 656,891 B | fp32 graph. Needs `onnx/model.onnx_data` (external weights) **in the same directory**. |
| `onnx/model.onnx_data` | 2,271,088,656 B | fp32 weights |
| `onnx/model_int8.onnx` | 570,727,094 B | dynamic int8, single file. Best fit for CPU. `model_quantized.onnx` is the same file (same LFS hash). |
| `onnx/model_uint8.onnx` | 570,727,094 B | uint8 variant |
| `onnx/model_fp16.onnx` | 1,136,209,678 B | fp16, mainly for GPU |
| `onnx/model_q4.onnx`, `model_q4f16.onnx`, `model_bnb4.onnx` | 0.7 to 1.25 GB | 4-bit variants, aimed at transformers.js/WebGPU. Avoid. |
| `tokenizer.json` | 17,082,900 B | for `tokenizers.Tokenizer.from_file` |
| `config.json`, `tokenizer_config.json`, `special_tokens_map.json` | small | |

Download URL pattern (pin the revision): `https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX/resolve/6f5ff65298512715a1e669753bc754d2bc8f367b/onnx/model_int8.onnx` (or `/resolve/main/...`). I checked each URL with a HEAD request on 2026-11-19. Each returns `302` to the CDN, with `x-linked-size` matching the sizes above and `x-repo-commit: 6f5ff652...`. Use the `etag` (the LFS sha256) to verify the download. For example, `model_int8.onnx` has etag `2957f6736473f43a7435c87e178fb1baff3d2e92ccb0c754c6ec959cecfc1e83`.

Other community exports exist (`woxpas-ai/bge-reranker-v2-m3-onnx`, `EmbeddedLLM/bge-reranker-v2-m3-onnx-o3-cpu`, and others). They are less established, so do not use them.

Graph I/O, checked from the strings in `onnx/model.onnx`:
- Inputs: `input_ids` and `attention_mask`, both int64 `[batch_size, sequence_length]`. There is **no** `token_type_ids` input.
- Output: `logits` float32 `[batch_size, 1]`. The config has a single label, `LABEL_0`.
- Score = `sigmoid(logit)`. This is what FlagEmbedding's `normalize=True` does. BAAI's examples give 5.26 → 0.9948 for a relevant pair and -8.19 → 0.00028 for an irrelevant one.

Tokenization with the `tokenizers` library:
- Encode pairs: `tok.encode_batch([(rule_text, bill_text), ...])`. The post-processor in `tokenizer.json` builds `<s> query </s></s> passage </s>`.
- Truncation: `max_position_embeddings` is 8194, so 8192 tokens is the hard ceiling. BAAI's examples use `max_length=512`. On CPU use 512 (or 1024), with `tok.enable_truncation(512, strategy="only_second")`.
- Padding: `tok.enable_padding(pad_id=1, pad_token="<pad>")`. The XLM-R pad id is 1. Build `attention_mask` from the encoding.
- Expect roughly 0.1 to 0.3 s per pair at 512 tokens with int8 on a few CPU cores. That is fine for 250 bills × 120 rules = 30,000 pairs/year, but a full rescore (300k pairs) takes hours. This estimate is not benchmarked.

## Sources (accessed 2026-11-19)

Microsoft:
- Model retirement schedule (updated 2026-11-18): https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule (redirect from https://learn.microsoft.com/en-us/azure/ai-foundry/concepts/model-lifecycle-retirement)
- Foundry Models sold by Azure (updated 2026-10-29): https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure
- Region availability (updated 2026-10-19): https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability
- Endpoints for Foundry Models (updated 2026-10-19), `ai.azure.com` scope: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints
- Configure keyless auth with Entra ID (2026-08-31), Cognitive Services User role: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-entra-id
- Quotas and limits (2026-10-05), 429 and `Retry-After`: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/quotas-limits
- Azure Retail Prices API, `productName eq 'Cohere Models'`: https://prices.azure.com/api/retail/prices
- Rerank 4.0 announcement (2025-12-11): https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/introducing-cohere-rerank-4-0-in-microsoft-foundry/4477076
- Rerank V5 announcement (2026-10-21, updated 2026-11-10): https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/cohere-rerank-v5-is-now-available-in-microsoft-foundry/4566311
- Catalog: https://ai.azure.com/catalog/models/Cohere-rerank-v5-pro, https://ai.azure.com/catalog/models/Cohere-rerank-v4.0-pro
- Q&A, v5 EU Data Zone (2026-10-23): https://learn.microsoft.com/en-us/answers/questions/6046549/data-zone-standard-eu-availability-for-cohere-embe
- Q&A, v4 Fast 500s and provider route (2026-02-24): https://learn.microsoft.com/en-us/answers/questions/5786029/cohere-rerank-v4-0-fast-returns-500-status-for-azu
- Q&A, v4 Fast 500 with DefaultAzureCredential (2026-03-11): https://learn.microsoft.com/en-us/answers/questions/5817093/cohere-rerank-v4-0-fast-returns-500-error-in-azure

Cohere:
- Rerank v2 API reference: https://docs.cohere.com/reference/rerank (Markdown: append `.md`)
- Rerank models, Rerank 5 details and limitations: https://docs.cohere.com/docs/rerank
- Models overview, including the platform name table: https://docs.cohere.com/docs/models
- Reranking best practices (limits, chunking, thresholds): https://docs.cohere.com/docs/reranking-best-practices
- Cohere on Azure (partly outdated: v1 URLs): https://docs.cohere.com/docs/cohere-on-microsoft-azure
- Rerank 5 launch (2026-10-21), calibration and thresholds: https://cohere.com/blog/rerank-5 and https://docs.cohere.com/changelog/rerank-v5
- Pricing FAQ (search unit definition): https://cohere.com/pricing

Community, for the endpoint and auth details:
- C# sample for v5 on Foundry (endpoint, Bearer, scope, role): https://github.com/taswar/Cohere-Rerank-V5-Pro-Fast-Csharp-Sample and https://taswar.zeytinsoft.com/cohere-rerank-v5-pro-and-fast-for-c-developers/ (2026-10-30)
- TrueFoundry gateway, Foundry rerank base URL: https://www.truefoundry.com/docs/ai-gateway/azure-ai-foundry
- llmgateway issue, `Cohere-rerank-v5-fast` request (2026-11-06): https://github.com/thekim1/llmgateway/issues/72

Hugging Face:
- https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX (API: https://huggingface.co/api/models/onnx-community/bge-reranker-v2-m3-ONNX/tree/main?recursive=true)
- https://huggingface.co/BAAI/bge-reranker-v2-m3
