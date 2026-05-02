import { ItemView, MarkdownRenderer, Notice, TFile, WorkspaceLeaf } from "obsidian";
import { CitationOut, KrunRagApi } from "./api";
import type KrunRagPlugin from "./main";

export const KRUN_RAG_VIEW_TYPE = "krun-rag-view";

interface Turn {
  question: string;
  citations: CitationOut[];
  answerEl: HTMLElement;
  metaEl: HTMLElement;
  rawAnswer: string;
  startedAt: number;
}

export class KrunRagView extends ItemView {
  private plugin: KrunRagPlugin;
  private api: KrunRagApi;
  private currentAbort: AbortController | null = null;

  // UI refs
  private statusEl!: HTMLElement;
  private convoEl!: HTMLElement;
  private inputEl!: HTMLTextAreaElement;
  private askBtn!: HTMLButtonElement;
  private activeNoteEl!: HTMLElement;

  constructor(leaf: WorkspaceLeaf, plugin: KrunRagPlugin) {
    super(leaf);
    this.plugin = plugin;
    this.api = new KrunRagApi(plugin.settings.serverUrl);
  }

  getViewType(): string {
    return KRUN_RAG_VIEW_TYPE;
  }
  getDisplayText(): string {
    return "KRUN RAG";
  }
  getIcon(): string {
    return "messages-square";
  }

  async onOpen(): Promise<void> {
    const root = this.containerEl.children[1];
    root.empty();
    root.addClass("krun-rag-view");

    // Header
    const header = root.createDiv({ cls: "krun-rag-header" });
    header.createEl("strong", { text: "KRUN RAG" });
    this.statusEl = header.createSpan({ cls: "krun-rag-status", text: "checking…" });

    // Toolbar
    const toolbar = root.createDiv({ cls: "krun-rag-toolbar" });
    const refreshBtn = toolbar.createEl("button", { text: "Health" });
    refreshBtn.onclick = () => this.refreshHealth();

    const reindexBtn = toolbar.createEl("button", { text: "Reindex (incremental)" });
    reindexBtn.onclick = () => this.runReindex();

    const clearBtn = toolbar.createEl("button", { text: "Clear" });
    clearBtn.onclick = () => this.clearConversation();

    // Active note
    this.activeNoteEl = root.createDiv({ cls: "krun-rag-active-note" });
    this.updateActiveNote();
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.updateActiveNote()));

    // Conversation
    this.convoEl = root.createDiv({ cls: "krun-rag-conversation" });
    this.convoEl.createDiv({ cls: "krun-rag-empty", text: "질문을 입력하세요. 출처를 클릭하면 노트로 이동합니다." });

    // Input
    const inputBox = root.createDiv({ cls: "krun-rag-input" });
    this.inputEl = inputBox.createEl("textarea", {
      attr: { placeholder: "예: 위밋모빌리티 1차DD 핵심 리스크" },
    });
    this.inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        this.submit();
      }
    });

    const inputRow = inputBox.createDiv({ cls: "krun-rag-input-row" });
    this.askBtn = inputRow.createEl("button", { text: "Ask (Ctrl/⌘+Enter)", cls: "mod-cta" });
    this.askBtn.onclick = () => this.submit();
    const stopBtn = inputRow.createEl("button", { text: "Stop" });
    stopBtn.onclick = () => this.cancel();

    this.refreshHealth();
  }

  async onClose(): Promise<void> {
    this.cancel();
  }

  /** Public: triggered by commands (palette / "ask about current note"). */
  focusAndPrefill(text: string): void {
    this.inputEl.value = text;
    this.inputEl.focus();
    this.inputEl.setSelectionRange(this.inputEl.value.length, this.inputEl.value.length);
  }

  rebindApi(): void {
    this.api.setBase(this.plugin.settings.serverUrl);
    this.refreshHealth();
  }

  // --- Internals ----------------------------------------------------------

  private updateActiveNote(): void {
    const f = this.app.workspace.getActiveFile();
    if (f) this.activeNoteEl.setText(`📄 ${f.path}`);
    else this.activeNoteEl.setText("📄 (no active note)");
  }

  private async refreshHealth(): Promise<void> {
    this.statusEl.setText("checking…");
    this.statusEl.removeClass("ok");
    this.statusEl.removeClass("bad");
    const h = await this.api.health();
    if (h.ok) {
      this.statusEl.addClass("ok");
      this.statusEl.setText(`● ${h.rows ?? 0} chunks`);
      this.statusEl.title = `vault=${h.vault_name ?? "?"}\nmodel=${h.gen_model ?? "?"}\nzdr=${h.zdr_enabled ? "on" : "off"}`;
    } else {
      this.statusEl.addClass("bad");
      this.statusEl.setText("● offline");
      this.statusEl.title = h.error ?? "server unreachable";
    }
  }

  private async runReindex(): Promise<void> {
    try {
      await this.api.reindex("incremental");
      new Notice("Reindex started (incremental)");
      setTimeout(() => this.refreshHealth(), 4000);
    } catch (e) {
      new Notice(`Reindex failed: ${e}`);
    }
  }

  private clearConversation(): void {
    this.convoEl.empty();
    this.convoEl.createDiv({ cls: "krun-rag-empty", text: "질문을 입력하세요." });
  }

  private cancel(): void {
    if (this.currentAbort) {
      this.currentAbort.abort();
      this.currentAbort = null;
      this.askBtn.disabled = false;
      this.askBtn.setText("Ask (Ctrl/⌘+Enter)");
    }
  }

  private submit(): void {
    const q = this.inputEl.value.trim();
    if (!q) return;
    if (this.currentAbort) {
      new Notice("이전 요청이 진행 중입니다.");
      return;
    }
    this.inputEl.value = "";

    // Strip empty placeholder if present
    const empty = this.convoEl.querySelector(".krun-rag-empty");
    if (empty) empty.remove();

    const turn = this.appendTurn(q);
    const activeFile = this.app.workspace.getActiveFile();
    const activeNote = this.plugin.settings.sendActiveNote && activeFile ? activeFile.path : null;

    this.askBtn.disabled = true;
    this.askBtn.setText("Streaming…");
    this.currentAbort = this.api.ask(
      {
        query: q,
        top_k: this.plugin.settings.topK,
        no_analyze: this.plugin.settings.noAnalyze,
        reranker: this.plugin.settings.rerankerEnabled,
        max_chunks_per_file: this.plugin.settings.maxChunksPerFile,
        active_note: activeNote,
      },
      {
        onAnalysis: (a) => {
          const bits: string[] = [];
          if (Array.isArray(a.companies) && a.companies.length) bits.push(`회사: ${(a.companies as string[]).join(", ")}`);
          if (Array.isArray(a.persons) && a.persons.length) bits.push(`인물: ${(a.persons as string[]).join(", ")}`);
          if (a.date_from || a.date_to) bits.push(`기간: ${a.date_from ?? "-"} → ${a.date_to ?? "-"}`);
          if (bits.length) turn.metaEl.setText("🔎 " + bits.join(" · "));
        },
        onCitations: (items) => {
          turn.citations = items;
          this.renderCitations(turn);
        },
        onDelta: (text) => {
          turn.rawAnswer += text;
          this.renderAnswerStreaming(turn);
        },
        onDone: (info) => {
          this.renderAnswerFinal(turn);
          const meta = turn.metaEl.textContent ?? "";
          turn.metaEl.setText(`${meta ? meta + " · " : ""}⏱ ${info.elapsed_seconds}s`);
          this.askBtn.disabled = false;
          this.askBtn.setText("Ask (Ctrl/⌘+Enter)");
          this.currentAbort = null;
        },
        onError: (msg) => {
          turn.answerEl.empty();
          turn.answerEl.createDiv({ cls: "krun-rag-error", text: `오류: ${msg}` });
          this.askBtn.disabled = false;
          this.askBtn.setText("Ask (Ctrl/⌘+Enter)");
          this.currentAbort = null;
        },
      },
    );
  }

  private appendTurn(question: string): Turn {
    const wrap = this.convoEl.createDiv({ cls: "krun-rag-turn" });
    wrap.createDiv({ cls: "krun-rag-turn-question", text: question });
    const meta = wrap.createDiv({ cls: "krun-rag-turn-meta" });
    const answer = wrap.createDiv({ cls: "krun-rag-turn-answer", text: "…" });
    const turn: Turn = {
      question,
      citations: [],
      answerEl: answer,
      metaEl: meta,
      rawAnswer: "",
      startedAt: Date.now(),
    };
    // Keep scrolled to bottom
    this.convoEl.scrollTop = this.convoEl.scrollHeight;
    return turn;
  }

  /** Streaming view: plain text + citation markers; no full markdown render until done. */
  private renderAnswerStreaming(turn: Turn): void {
    turn.answerEl.empty();
    const text = turn.rawAnswer;
    const re = /\[(\d+)\]/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text))) {
      if (m.index > last) turn.answerEl.appendText(text.slice(last, m.index));
      const n = Number(m[1]);
      const link = turn.answerEl.createSpan({ cls: "krun-rag-cite", text: `[${n}]` });
      link.onclick = () => this.openCitation(turn, n);
      last = m.index + m[0].length;
    }
    if (last < text.length) turn.answerEl.appendText(text.slice(last));
    this.convoEl.scrollTop = this.convoEl.scrollHeight;
  }

  /** Final render: full markdown via Obsidian's renderer + clickable [n] tokens. */
  private async renderAnswerFinal(turn: Turn): Promise<void> {
    turn.answerEl.empty();
    const md = turn.answerEl.createDiv();
    await MarkdownRenderer.render(this.app, turn.rawAnswer, md, "", this.plugin);
    // Replace [n] occurrences inside text nodes with clickable spans.
    this.linkifyCitations(md, turn);
    this.renderCitations(turn);
  }

  private linkifyCitations(root: HTMLElement, turn: Turn): void {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const targets: Text[] = [];
    let n: Node | null;
    while ((n = walker.nextNode())) {
      if ((n as Text).data.match(/\[\d+\]/)) targets.push(n as Text);
    }
    for (const node of targets) {
      const parent = node.parentNode;
      if (!parent) continue;
      const re = /\[(\d+)\]/g;
      const text = node.data;
      let last = 0;
      let m: RegExpExecArray | null;
      const frag = document.createDocumentFragment();
      while ((m = re.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const num = Number(m[1]);
        const span = document.createElement("span");
        span.className = "krun-rag-cite";
        span.textContent = `[${num}]`;
        span.onclick = () => this.openCitation(turn, num);
        frag.appendChild(span);
        last = m.index + m[0].length;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      parent.replaceChild(frag, node);
    }
  }

  private renderCitations(turn: Turn): void {
    // Drop and re-render the citations block at the bottom of this turn
    const old = turn.answerEl.parentElement?.querySelector(".krun-rag-citations");
    if (old) old.remove();
    if (!turn.citations.length) return;
    const block = turn.answerEl.parentElement!.createDiv({ cls: "krun-rag-citations" });
    block.createDiv({ cls: "krun-rag-citations-header", text: `Sources (${turn.citations.length})` });
    for (const c of turn.citations) {
      const row = block.createDiv({ cls: "krun-rag-citation" });
      row.createDiv({ cls: "krun-rag-citation-title", text: `[${c.n}] ${c.title}${c.header_path ? " > " + c.header_path : ""}` });
      const metaBits: string[] = [c.doc_type];
      if (c.company) metaBits.push(c.company);
      if (c.person) metaBits.push(c.person);
      if (c.date) metaBits.push(c.date);
      row.createDiv({ cls: "krun-rag-citation-meta", text: metaBits.join(" · ") + " — " + c.vault_relative });
      if (c.snippet) row.createDiv({ cls: "krun-rag-citation-snippet", text: c.snippet });
      row.onclick = () => this.openCitationByPath(c.vault_relative);
    }
  }

  private openCitation(turn: Turn, n: number): void {
    const c = turn.citations.find((x) => x.n === n);
    if (!c) {
      new Notice(`인용 [${n}]을 찾을 수 없습니다.`);
      return;
    }
    this.openCitationByPath(c.vault_relative);
  }

  private openCitationByPath(vaultRelative: string): void {
    if (!vaultRelative) return;
    // Try exact-path resolution first.
    const f = this.app.vault.getAbstractFileByPath(vaultRelative);
    if (f instanceof TFile) {
      this.app.workspace.getLeaf(false).openFile(f);
      return;
    }
    // Fallback: link resolution by stem (handles vault-name mismatches).
    this.app.workspace.openLinkText(vaultRelative.replace(/\.md$/, ""), "", false);
  }
}
