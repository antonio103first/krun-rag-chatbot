import { ItemView, MarkdownRenderer, Menu, Notice, TFile, WorkspaceLeaf } from "obsidian";
import { AskRequest, CitationOut, KrunRagApi } from "./api";
import type KrunRagPlugin from "./main";

export const KRUN_RAG_VIEW_TYPE = "krun-rag-view";

interface Turn {
  question: string;
  citations: CitationOut[];
  answerEl: HTMLElement;
  metaEl: HTMLElement;
  rawAnswer: string;
  startedAt: number;
  usedIndices?: number[]; // populated on `done`; restricts citation cards to ones the answer actually cites
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
    // The status pill doubles as the on/off control: click it to start
    // (run_api.bat) when offline or shut down when online.
    this.statusEl = header.createSpan({ cls: "krun-rag-status", text: "checking…" });
    this.statusEl.style.cursor = "pointer";
    this.statusEl.onclick = (e) => this.openServerMenu(e);

    // Primary actions — the two things that aren't "type a question".
    const actions = root.createDiv({ cls: "krun-rag-actions" });
    const briefBtn = actions.createEl("button", { text: "🏢 회사 브리핑", cls: "mod-cta" });
    briefBtn.onclick = () => this.plugin.openCompanyPicker(this);

    const noteBtn = actions.createEl("button", { text: "📄 현재 노트" });
    noteBtn.onclick = () => this.askAboutCurrentNote();

    // Housekeeping — deliberately smaller and below the primary row.
    const toolbar = root.createDiv({ cls: "krun-rag-toolbar" });
    const refreshBtn = toolbar.createEl("button", { text: "상태" });
    refreshBtn.onclick = () => this.refreshHealth();

    const reindexBtn = toolbar.createEl("button", { text: "재색인" });
    reindexBtn.onclick = () => this.runReindex();

    const clearBtn = toolbar.createEl("button", { text: "대화 지우기" });
    clearBtn.onclick = () => this.clearConversation();

    // Active note
    this.activeNoteEl = root.createDiv({ cls: "krun-rag-active-note" });
    this.updateActiveNote();
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.updateActiveNote()));

    // Conversation
    this.convoEl = root.createDiv({ cls: "krun-rag-conversation" });
    this.convoEl.createDiv({
      cls: "krun-rag-empty",
      text:
        "질문을 입력하고 Ctrl+Enter. 미팅 전이라면 위 「회사 브리핑」을 누르세요. " +
        "답변의 [1] 같은 번호를 클릭하면 근거 노트가 열립니다.",
    });

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

  /** Ask about whatever note is open. No-op with a nudge when none is. */
  private askAboutCurrentNote(): void {
    const f = this.app.workspace.getActiveFile();
    if (!f) {
      new Notice("열려 있는 노트가 없습니다.");
      return;
    }
    this.run(`[[${f.basename}]]에 대해 알려줘 — 핵심 요약과 다음에 챙길 점은?`, {});
  }

  /** Public: triggered by the palette command.
   *
   * Guarded: a caller can reach us before onOpen() has built the DOM (deferred
   * leaves resolve asynchronously), and an unguarded throw here is swallowed by
   * the caller's promise chain — which reads to the user as "nothing happened".
   */
  focusAndPrefill(text: string): void {
    if (!this.inputEl) return;
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
      this.statusEl.title = `vault=${h.vault_name ?? "?"}\nmodel=${h.gen_model ?? "?"}\nzdr=${h.zdr_enabled ? "on" : "off"}\n\n클릭: 서버 시작/종료`;
    } else {
      this.statusEl.addClass("bad");
      this.statusEl.setText("● offline");
      this.statusEl.title = (h.error ?? "server unreachable") + "\n\n클릭: 서버 시작";
    }
  }

  /** Status-pill menu: start (run_api.bat) when offline, shut down when online. */
  private openServerMenu(evt: MouseEvent): void {
    const online = this.statusEl.hasClass("ok");
    const menu = new Menu();
    if (online) {
      menu.addItem((i) =>
        i.setTitle("서버 종료").setIcon("power").onClick(() => this.shutdownServer()),
      );
    } else {
      menu.addItem((i) =>
        i.setTitle("서버 시작 (run_api.bat)").setIcon("play").onClick(() => this.startServer()),
      );
    }
    menu.addSeparator();
    menu.addItem((i) =>
      i.setTitle("상태 새로고침").setIcon("refresh-cw").onClick(() => this.refreshHealth()),
    );
    menu.showAtMouseEvent(evt);
  }

  /** Launch the RAG server in the background.
   *
   * Desktop-only (manifest isDesktopOnly=true). We launch the venv Python
   * DIRECTLY rather than the .bat: Obsidian's process env frequently lacks
   * ~/.local/bin, so the bat's `uv run uvicorn` fails silently and the pill
   * sticks on "starting…" (verified by reproducing with a scrubbed PATH). The
   * venv interpreter is self-contained — no `uv`, no PATH, no cmd quote-strip on
   * the spaced path. We set the HF-offline env here to match run_api.bat. Falls
   * back to the .bat only if the venv python isn't found.
   */
  private startServer(): void {
    const script = this.plugin.settings.serverScriptPath?.trim();
    if (!script) {
      new Notice("run_api.bat 경로가 비어 있습니다. 설정에서 지정하세요.");
      return;
    }
    try {
      // Node modules are available in Obsidian's Electron desktop runtime.
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const cp = require("child_process");
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const nodePath = require("path");
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const fs = require("fs");

      // <root>/scripts/run_api.bat → <root>/.venv/Scripts/python.exe
      const root = nodePath.dirname(nodePath.dirname(script));
      const python = nodePath.join(root, ".venv", "Scripts", "python.exe");

      // Where to bind — parsed from the configured server URL.
      let host = "127.0.0.1";
      let port = "8765";
      try {
        const u = new URL(this.plugin.settings.serverUrl);
        host = u.hostname || host;
        port = u.port || port;
      } catch {
        /* keep defaults */
      }

      let child;
      if (fs.existsSync(python)) {
        child = cp.spawn(
          python,
          ["-m", "uvicorn", "apps.fastapi_server:app", "--host", host, "--port", port],
          {
            cwd: root,
            env: { ...process.env, HF_HUB_OFFLINE: "1", TRANSFORMERS_OFFLINE: "1" },
            detached: true,
            stdio: "ignore",
            windowsHide: true,
          },
        );
      } else {
        // Fallback: run the bat. shell:true + pre-quoted path survives the space
        // (Node emits `cmd /d /s /c ""<path>""`; /s strips only the outer pair).
        child = cp.spawn(`"${script}"`, {
          cwd: nodePath.dirname(script),
          shell: true,
          detached: true,
          stdio: "ignore",
          windowsHide: true,
        });
      }
      child.on("error", (e: Error) => new Notice(`서버 시작 실패: ${e.message}`));
      child.unref();
      new Notice("KRUN RAG 서버를 백그라운드로 시작합니다… (수 초 대기)");
      this.statusEl.setText("● starting…");
      this.pollUntilOnline();
    } catch (e) {
      new Notice(`서버 시작 실패: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /** Poll /health after a start until it responds ok (or we give up). */
  private pollUntilOnline(): void {
    let n = 0;
    const max = 20; // ~30s: model warmup can take several seconds
    const tick = async () => {
      n++;
      const h = await this.api.health();
      if (h.ok) {
        await this.refreshHealth();
        new Notice("✅ KRUN RAG 서버 온라인");
        return;
      }
      if (n >= max) {
        await this.refreshHealth();
        new Notice("서버가 아직 응답하지 않습니다. 열린 콘솔 창을 확인하세요.");
        return;
      }
      window.setTimeout(tick, 1500);
    };
    window.setTimeout(tick, 2500);
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

  /** Turn the RAG server off. Confirms first — an accidental click means the
   *  user has to relaunch run_api.bat by hand. */
  private async shutdownServer(): Promise<void> {
    const ok = window.confirm("KRUN RAG 서버를 종료할까요?\n다시 사용하려면 run_api.bat 을 실행해야 합니다.");
    if (!ok) return;
    const res = await this.api.shutdown();
    if (res.ok) {
      new Notice("KRUN RAG 서버를 종료했습니다.");
    } else {
      new Notice(`종료하지 못했습니다: ${res.message ?? "서버 응답 없음"}`);
    }
    // The server exits ~0.3s after acking; let health flip to offline.
    setTimeout(() => this.refreshHealth(), 1500);
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
    this.inputEl.value = "";
    this.run(q, {});
  }

  /** Public: pre-meeting briefing for one company (command palette).
   *
   * Sends `mode: "company_brief"` so the server skips the analyzer — a bare
   * company name would otherwise yield no filter at all.
   */
  runCompanyBrief(company: string): void {
    this.run(`${company} — 미팅 전 브리핑`, { mode: "company_brief", company });
  }

  private run(question: string, extra: Partial<AskRequest>): void {
    if (this.currentAbort) {
      new Notice("이전 요청이 진행 중입니다.");
      return;
    }

    // Strip empty placeholder if present
    const empty = this.convoEl.querySelector(".krun-rag-empty");
    if (empty) empty.remove();

    const turn = this.appendTurn(question);
    const activeFile = this.app.workspace.getActiveFile();
    const activeNote = this.plugin.settings.sendActiveNote && activeFile ? activeFile.path : null;

    this.askBtn.disabled = true;
    this.askBtn.setText("Streaming…");
    this.currentAbort = this.api.ask(
      {
        query: question,
        top_k: this.plugin.settings.topK,
        no_analyze: this.plugin.settings.noAnalyze,
        reranker: this.plugin.settings.rerankerEnabled,
        max_chunks_per_file: this.plugin.settings.maxChunksPerFile,
        active_note: activeNote,
        ...extra,
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
          turn.usedIndices = info.used_citation_indices ?? [];
          this.renderAnswerFinal(turn);
          this.renderCitations(turn); // re-render: hides unused cards now that we know which [n] were cited
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
    // Per-turn toolbar with Copy buttons (rendered after answer is final).
    const toolbar = wrap.createDiv({ cls: "krun-rag-turn-toolbar" });
    const copyAnswerBtn = toolbar.createEl("button", { text: "📋 답변" });
    copyAnswerBtn.title = "답변 본문 복사";
    copyAnswerBtn.onclick = (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(turn.rawAnswer || "");
      new Notice("답변 복사됨");
    };
    const copyAllBtn = toolbar.createEl("button", { text: "📋 Q+A+출처" });
    copyAllBtn.title = "질문 + 답변 + 출처 마크다운으로 복사";
    copyAllBtn.onclick = (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(this.formatTurnMarkdown(turn));
      new Notice("Q+A+출처 복사됨");
    };
    // Keep scrolled to bottom
    this.convoEl.scrollTop = this.convoEl.scrollHeight;
    return turn;
  }

  /** Render Q + A + citations as a portable markdown blob for copy/paste. */
  private formatTurnMarkdown(turn: Turn): string {
    const lines: string[] = [];
    lines.push(`### Q: ${turn.question}`);
    lines.push("");
    lines.push(turn.rawAnswer.trim() || "(no answer)");
    if (turn.citations.length) {
      lines.push("");
      lines.push("**Sources:**");
      for (const c of turn.citations) {
        const meta = [c.doc_type, c.company, c.person, c.date].filter(Boolean).join(" · ");
        lines.push(`- [${c.n}] ${c.title}${c.header_path ? " > " + c.header_path : ""}  \n  ${meta} — \`${c.vault_relative}\``);
      }
    }
    return lines.join("\n");
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
    // After `done`, restrict to the citations the answer actually cites.
    // Before `done` (during streaming), show all retrieved cards.
    const visible = turn.usedIndices && turn.usedIndices.length
      ? turn.citations.filter((c) => turn.usedIndices!.includes(c.n))
      : turn.citations;
    if (!visible.length) return;
    const block = turn.answerEl.parentElement!.createDiv({ cls: "krun-rag-citations" });
    const total = turn.citations.length;
    const headerText = visible.length < total
      ? `Sources (${visible.length} cited / ${total} retrieved)`
      : `Sources (${total})`;
    block.createDiv({ cls: "krun-rag-citations-header", text: headerText });
    for (const c of visible) {
      const row = block.createDiv({ cls: "krun-rag-citation" });
      // Only the title is clickable — meta/snippet are plain selectable text
      // so the user can drag-select inside the citation card without
      // accidentally opening the file.
      const titleEl = row.createDiv({
        cls: "krun-rag-citation-title",
        text: `[${c.n}] ${c.title}${c.header_path ? " > " + c.header_path : ""}`,
      });
      titleEl.onclick = () => this.openCitationByPath(c.vault_relative);
      const metaBits: string[] = [c.doc_type];
      if (c.company) metaBits.push(c.company);
      if (c.person) metaBits.push(c.person);
      if (c.date) metaBits.push(c.date);
      row.createDiv({ cls: "krun-rag-citation-meta", text: metaBits.join(" · ") + " — " + c.vault_relative });
      if (c.snippet) row.createDiv({ cls: "krun-rag-citation-snippet", text: c.snippet });
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
