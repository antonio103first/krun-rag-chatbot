import { Notice, Plugin, WorkspaceLeaf } from "obsidian";
import { KrunRagApi } from "./api";
import { CompanyPickerModal } from "./company-modal";
import { DEFAULT_SETTINGS, KrunRagSettings, KrunRagSettingTab } from "./settings";
import { KRUN_RAG_VIEW_TYPE, KrunRagView } from "./view";

export default class KrunRagPlugin extends Plugin {
  settings: KrunRagSettings = DEFAULT_SETTINGS;

  async onload(): Promise<void> {
    await this.loadSettings();

    this.registerView(KRUN_RAG_VIEW_TYPE, (leaf) => new KrunRagView(leaf, this));

    this.addRibbonIcon("messages-square", "KRUN RAG 검색", () => this.activateView());

    // ONE command. Everything else — company briefing, asking about the current
    // note, reindexing — lives as a button inside the sidebar. Five palette
    // entries competing with the user's other Ctrl+P commands was the problem;
    // one verb they can remember plus in-place buttons is the fix.
    this.addCommand({
      id: "open-view",
      // Named so that "krun", "rag", and "검색" all surface it in the palette —
      // the plugin is listed as "KRUN RAG", so a 검색-only name was unfindable
      // for anyone who remembered the plugin's name instead of the verb.
      name: "KRUN RAG 검색",
      callback: () => this.activateView().then((view) => view?.focusAndPrefill("")),
    });

    this.addSettingTab(new KrunRagSettingTab(this.app, this));
  }

  /** Open the company picker, pre-filtered to the active file's company folder. */
  async openCompanyPicker(view: KrunRagView): Promise<void> {
    const entries = await new KrunRagApi(this.settings.serverUrl).companies();
    if (!entries.length) {
      new Notice("회사 목록을 가져오지 못했습니다. RAG 서버가 실행 중인지 확인하세요.");
      return;
    }
    const modal = new CompanyPickerModal(this.app, entries, (c) => view.runCompanyBrief(c));
    modal.open();
    const guess = this.guessCompanyFromActiveFile();
    if (guess) {
      modal.inputEl.value = guess;
      modal.inputEl.dispatchEvent(new Event("input"));
    }
  }

  onunload(): void {
    // Obsidian unmounts views automatically; ItemView.onClose aborts in-flight streams.
  }

  /** Company folder name if the active file lives under 03_Companies/, else null.
   *
   * Vault layout is 03_Companies/{Antonio|KRUN}/{단계}/{회사}/{파일}.md, so the
   * containing folder is the company for both the profile note and its meetings.
   */
  guessCompanyFromActiveFile(): string | null {
    const f = this.app.workspace.getActiveFile();
    if (!f) return null;
    const parts = f.path.split("/");
    if (parts[0] !== "03_Companies" || parts.length < 3) return null;
    return parts[parts.length - 2] || null;
  }

  async loadSettings(): Promise<void> {
    this.settings = { ...DEFAULT_SETTINGS, ...(await this.loadData()) };
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
    // Push base URL change to any open view.
    for (const leaf of this.app.workspace.getLeavesOfType(KRUN_RAG_VIEW_TYPE)) {
      const v = leaf.view;
      if (v instanceof KrunRagView) v.rebindApi();
    }
  }

  async activateView(): Promise<KrunRagView | null> {
    const { workspace } = this.app;
    try {
      let leaf: WorkspaceLeaf | null = workspace.getLeavesOfType(KRUN_RAG_VIEW_TYPE)[0] ?? null;
      if (!leaf) {
        // getRightLeaf(false) returns null when the right split is collapsed or
        // absent; asking for a new split is the documented fallback.
        leaf = workspace.getRightLeaf(false) ?? workspace.getRightLeaf(true);
        if (!leaf) {
          new Notice("사이드바를 열 수 없습니다. 오른쪽 패널이 닫혀 있는지 확인하세요.");
          return null;
        }
        await leaf.setViewState({ type: KRUN_RAG_VIEW_TYPE, active: true });
      }

      await workspace.revealLeaf(leaf);

      // Obsidian 1.7+ restores sidebar leaves in a *deferred* state: the leaf
      // exists (ours is persisted in workspace.json) but `leaf.view` is a
      // placeholder, so `instanceof KrunRagView` is false and we used to return
      // null — the command then did nothing at all, with no error to show for
      // it. Force the view to materialize before touching it.
      if (leaf.isDeferred) await leaf.loadIfDeferred();

      const view = leaf.view;
      if (view instanceof KrunRagView) return view;
      new Notice("KRUN RAG 화면을 불러오지 못했습니다. Obsidian을 재시작해 주세요.");
      return null;
    } catch (e) {
      new Notice(`KRUN RAG 열기 실패: ${e instanceof Error ? e.message : String(e)}`);
      return null;
    }
  }
}
