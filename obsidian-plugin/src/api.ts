/* SSE-style streaming client for the KRUN RAG FastAPI server.
 * fetch() with a ReadableStream — no EventSource because we POST a JSON body.
 */

export interface CitationOut {
  n: number;
  title: string;
  vault_relative: string;
  header_path: string;
  obsidian_uri: string;
  doc_type: string;
  company: string | null;
  person: string | null;
  date: string | null;
  snippet: string;
}

export interface AskRequest {
  query: string;
  top_k?: number;
  no_analyze?: boolean;
  reranker?: boolean | null;
  max_chunks_per_file?: number;
  active_note?: string | null;
}

export interface AskCallbacks {
  onAnalysis?: (a: Record<string, unknown>) => void;
  onCitations?: (items: CitationOut[], whereClause: string | null) => void;
  onDelta?: (text: string) => void;
  onDone?: (info: { elapsed_seconds: number; answer_length?: number }) => void;
  onError?: (msg: string) => void;
}

export interface HealthResponse {
  ok: boolean;
  rows?: number;
  vault?: string;
  vault_name?: string;
  gen_model?: string;
  analyzer_model?: string;
  zdr_enabled?: boolean;
  reindex_running?: boolean;
  error?: string;
}

export class KrunRagApi {
  constructor(private baseUrl: string) {}

  setBase(url: string) {
    this.baseUrl = url.replace(/\/$/, "");
  }

  async health(): Promise<HealthResponse> {
    try {
      const r = await fetch(`${this.baseUrl}/health`, { method: "GET" });
      return (await r.json()) as HealthResponse;
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  async reindex(mode: "incremental" | "full" | "refresh-metadata" = "incremental"): Promise<void> {
    const r = await fetch(`${this.baseUrl}/reindex`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (!r.ok) throw new Error(`reindex failed: ${r.status} ${await r.text()}`);
  }

  /** Ask a question; streams events via callbacks. Returns an AbortController. */
  ask(req: AskRequest, cb: AskCallbacks): AbortController {
    const ctrl = new AbortController();
    this.askInner(req, cb, ctrl).catch((e) => {
      if ((e as Error).name === "AbortError") return;
      cb.onError?.(String(e));
    });
    return ctrl;
  }

  private async askInner(req: AskRequest, cb: AskCallbacks, ctrl: AbortController): Promise<void> {
    const r = await fetch(`${this.baseUrl}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(req),
      signal: ctrl.signal,
    });
    if (!r.ok || !r.body) {
      cb.onError?.(`server returned ${r.status}: ${await r.text().catch(() => "")}`);
      return;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE frames separated by blank line
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        this.dispatchFrame(frame, cb);
      }
    }
    if (buf.trim()) this.dispatchFrame(buf, cb);
  }

  private dispatchFrame(frame: string, cb: AskCallbacks): void {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    const raw = dataLines.join("\n");
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch {
      data = raw;
    }
    switch (event) {
      case "analysis":
        cb.onAnalysis?.(data as Record<string, unknown>);
        break;
      case "citations": {
        const d = data as { items: CitationOut[]; where_clause: string | null };
        cb.onCitations?.(d.items, d.where_clause);
        break;
      }
      case "delta":
        cb.onDelta?.((data as { text: string }).text);
        break;
      case "done":
        cb.onDone?.(data as { elapsed_seconds: number });
        break;
      case "error":
        cb.onError?.((data as { message: string }).message);
        break;
    }
  }
}
