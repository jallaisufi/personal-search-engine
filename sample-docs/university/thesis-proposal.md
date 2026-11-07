# Master's Thesis Proposal

Working title: Evaluating Hybrid Retrieval for Retrieval-Augmented Generation over Personal Documents.

## Motivation

Large language models answer questions fluently but cannot see a person's own files. Retrieval-augmented generation (RAG) fixes this by first retrieving relevant passages and then letting the model answer from them. The quality of the answer depends heavily on retrieval.

## Research Questions

1. Does combining dense vector search with keyword search (hybrid retrieval) improve recall@10 over dense retrieval alone on personal document collections?
2. How much does a cross-encoder reranking step improve answer faithfulness, and at what latency cost?
3. Which chunking strategy (fixed size, heading-aware, sentence-window) works best for mixed PDF and Word collections?

## Method

Build a test collection of 2,000 personal documents with 300 labelled questions. Compare BM25, dense retrieval, and Reciprocal Rank Fusion of both. Measure recall@10, MRR, answer faithfulness, and p95 latency.

## Timeline

- Literature review finished by 15 December 2026.
- Prototype and test collection ready by end of February 2027.
- Thesis submission deadline: 30 June 2027.

Supervisor: Prof. Dr. Weber. Second reviewer to be confirmed.
