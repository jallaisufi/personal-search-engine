// Load test for the search API (k6).
//
//   SCENARIO=cold   every query is unique, so nothing is served from the result cache:
//                   measures embedding + the hybrid database query
//   SCENARIO=warm   a small set of queries repeated: measures the Redis cache path
//
// Run through `make bench` (see bench/README.md).
import http from "k6/http";
import { check } from "k6";
import { Counter, Rate } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://gateway";
const SCENARIO = __ENV.SCENARIO || "warm";
const LABEL = __ENV.LABEL || "lite";

const cacheHits = new Rate("cache_hits");
const servedBy = new Counter("served_by"); // tagged by API replica

const QUERIES = [
  "security deposit",
  "notice period for the tenant",
  "when is the design review",
  "mTLS rollout plan",
  "canary rollout traffic",
  "tax return submission",
  "home office days",
  "thesis submission deadline",
  "hybrid retrieval recall",
  "chunking strategy",
  "hotel cancellation",
  "train to Darmstadt",
  "pgvector in production",
  "on-call shadow rotation",
  "probation review",
  "rent increase rules",
  "subletting permission",
  "read replica for reporting",
  "work equipment receipts",
  "literature review",
];

export const options = {
  scenarios: {
    search: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 20),
      duration: __ENV.DURATION || "30s",
    },
  },
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
};

export default function () {
  let query = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  if (SCENARIO === "cold") query = `${query} ${__VU}x${__ITER}`; // unique: never cached

  const response = http.get(`${BASE}/api/search?q=${encodeURIComponent(query)}`);
  check(response, { "status is 200": (r) => r.status === 200 });
  cacheHits.add(response.headers["X-Cache"] === "HIT");
  servedBy.add(1, { replica: response.headers["X-Served-By"] || "unknown" });
}

export function handleSummary(data) {
  const m = data.metrics;
  const duration = m.http_req_duration.values;
  const row = {
    label: LABEL,
    scenario: SCENARIO,
    vus: Number(__ENV.VUS || 20),
    requests: m.http_reqs.values.count,
    requests_per_second: m.http_reqs.values.rate,
    p50_ms: duration.med,
    p95_ms: duration["p(95)"],
    p99_ms: duration["p(99)"],
    cache_hit_rate: m.cache_hits ? m.cache_hits.values.rate : 0,
    failed_rate: m.http_req_failed.values.rate,
  };
  const line =
    `${LABEL}/${SCENARIO}: ${row.requests_per_second.toFixed(0)} req/s, ` +
    `p50 ${row.p50_ms.toFixed(1)} ms, p95 ${row.p95_ms.toFixed(1)} ms, ` +
    `p99 ${row.p99_ms.toFixed(1)} ms, cache hits ${(row.cache_hit_rate * 100).toFixed(0)}%, ` +
    `failed ${(row.failed_rate * 100).toFixed(2)}%\n`;
  return {
    stdout: line,
    [`/bench/results/${LABEL}-${SCENARIO}.json`]: JSON.stringify(row, null, 2),
  };
}
