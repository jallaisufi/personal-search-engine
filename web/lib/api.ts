// Types mirror the FastAPI response models in backend/app/api/routes.py.

export type SearchResult = {
  chunk_id: number;
  document_id: number;
  title: string;
  path: string;
  web_url: string | null;
  page: number | null;
  heading_path: string | null;
  char_start: number;
  char_end: number;
  doc_length: number;
  /** HTML-escaped on the server; only <mark> tags are added. */
  snippet_html: string;
  score: number;
  matched_by: ("semantic" | "keyword")[];
};

export type SearchResponse = {
  query: string;
  took_ms: number;
  /** true when the whole result came from the Redis cache */
  cached: boolean;
  results: SearchResult[];
};

export type DocumentSummary = {
  id: number;
  title: string;
  path: string;
  status: "pending" | "indexed" | "failed";
  chunks: number;
  modified_at: string | null;
  indexed_at: string | null;
  error: string | null;
};

export type DocumentDetail = {
  id: number;
  title: string;
  path: string;
  web_url: string | null;
  mime_type: string | null;
  status: string;
  modified_at: string | null;
  text: string;
  chunks: {
    id: number;
    char_start: number;
    char_end: number;
    page: number | null;
    heading_path: string | null;
  }[];
};

export type Stats = {
  documents: Record<string, number>;
  chunks: number;
  index_version: number;
  embedding_model: string | null;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { signal, headers: { Accept: "application/json" } });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("The search service didn't respond. Check that the stack is running.");
  }
  if (!response.ok) {
    throw new ApiError(`The search service returned an error (${response.status}).`, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  search: (q: string, signal?: AbortSignal) =>
    get<SearchResponse>(`/search?${new URLSearchParams({ q, limit: "20" })}`, signal),
  document: (id: number, signal?: AbortSignal) => get<DocumentDetail>(`/documents/${id}`, signal),
  documents: () => get<DocumentSummary[]>("/documents"),
  stats: () => get<Stats>("/stats"),
};

/** "Lease > Rent and Payments" → ["Lease", "Rent and Payments"] */
export function headingTrail(path: string | null): string[] {
  return path ? path.split(" > ") : [];
}

export type Source = SearchResult & { n: number };

export type AskEvent =
  | { event: "sources"; data: Source[] }
  | { event: "token"; data: { text: string } }
  | { event: "done"; data: { cited: number[]; took_ms: number; cached: boolean } }
  | { event: "error"; data: { message: string } };

/** POST /api/ask and call `onEvent` for each server-sent event as it arrives. */
export async function askStream(
  question: string,
  onEvent: (event: AskEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question }),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("The answer service didn't respond. Check that the stack is running.");
  }
  if (response.status === 429) {
    throw new ApiError("Too many questions in a short time. Wait a moment and ask again.", 429);
  }
  if (!response.ok || !response.body) {
    throw new ApiError(`The answer service returned an error (${response.status}).`, response.status);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (event && data) onEvent({ event, data: JSON.parse(data) } as AskEvent);
    }
  }
}
